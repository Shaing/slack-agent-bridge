"""Slack-agnostic driver around ClaudeSDKClient.

One `run_turn` = one Claude Code turn (a fresh CLI subprocess, resumed from a
stored session id). Output is pushed through a `TurnSink`; permission prompts
and clarifying questions are answered through a `Prompter`.

The CLI outlives its first result when background tasks are running (Agent or
Bash with run_in_background): each finished task injects another turn, marked
by `ResultMessage.origin`. The runner keeps the CLI open until those are done,
and skips injected results when deciding whether the prompt was answered — on
resume, a task killed with the previous CLI replays as an injected turn that
arrives before the prompt's own result.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    ResultError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import (
    TERMINAL_TASK_STATUSES,
    PermissionMode,
    PermissionResultAllow,
    PermissionResultDeny,
    TaskNotificationMessage,
    TaskUpdatedMessage,
    ToolPermissionContext,
)

log = logging.getLogger(__name__)

SESSION_NOT_FOUND_RE = re.compile(r"no conversation found|session.*not found|could not find session", re.I)

_WAKE = object()  # queued by TurnHandle.interrupt while only background tasks are left
_END = object()  # the CLI's message stream ended


@dataclass
class _PumpError:
    exc: BaseException


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #
@dataclass
class TurnRequest:
    thread_key: str
    prompt: str
    cwd: str
    session_id: str | None = None
    permission_mode: str = "default"
    model: str | None = None
    effort: str | None = None  # None = the CLI's default for the model


@dataclass
class TurnResult:
    session_id: str | None
    subtype: str
    is_error: bool
    result_text: str
    total_cost_usd: float | None = None
    duration_ms: int = 0
    num_turns: int = 0
    error: str | None = None
    stderr_tail: str = ""


@dataclass
class Decision:
    kind: Literal["allow", "always", "deny"]
    by_user: str = ""
    message: str = ""


@dataclass
class QuestionAnswer:
    answers: dict[str, Any] | None = None
    response: str | None = None
    by_user: str = ""


@dataclass
class TurnHandle:
    """Lets the caller interrupt a running turn."""

    client: ClaudeSDKClient | None = None
    interrupted: bool = False
    idle: bool = False  # the prompt is answered; the CLI stays open only for background tasks
    wake: Callable[[], None] | None = None

    async def interrupt(self) -> bool:
        if self.client is None:
            return False
        self.interrupted = True
        if self.idle and self.wake is not None:
            self.wake()  # nothing to interrupt in the CLI; closing it ends the background tasks
            return True
        try:
            await self.client.interrupt()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("interrupt failed: %s", exc)
            return False

    async def set_permission_mode(self, mode: str) -> bool:
        """Switch the running turn's permission mode. False if nothing is running."""
        if self.client is None:
            return False
        try:
            await self.client.set_permission_mode(cast(PermissionMode, mode))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("set_permission_mode failed: %s", exc)
            return False

    async def set_model(self, model: str | None) -> bool:
        """Switch the running turn's model (None = default). False if nothing is running."""
        if self.client is None:
            return False
        try:
            await self.client.set_model(model)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("set_model failed: %s", exc)
            return False


class TurnSink(Protocol):
    async def on_session_started(self, session_id: str) -> None: ...
    async def on_text(self, segment: int, text: str) -> None: ...
    async def on_tool_use(self, tool_use_id: str, name: str, inp: dict[str, Any], subagent: bool) -> None: ...
    async def on_tool_result(self, tool_use_id: str, is_error: bool) -> None: ...
    async def on_waiting(self, kind: str, tool_name: str) -> None: ...
    async def on_background(self, tasks: list[str]) -> None: ...
    async def on_working(self) -> None: ...
    async def on_notice(self, text: str) -> None: ...
    async def on_result(self, result: TurnResult) -> None: ...


class Prompter(Protocol):
    async def ask_permission(
        self, thread_key: str, tool_name: str, input_data: dict[str, Any], context: ToolPermissionContext
    ) -> Decision: ...
    async def ask_question(self, thread_key: str, questions: list[dict[str, Any]]) -> QuestionAnswer: ...


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
@dataclass
class Runner:
    cli_path: str | None = None
    model: str | None = None
    max_concurrent: int = 3
    stream_deltas: bool = False
    turn_timeout_s: float = 0
    background_wait_s: float = 3600
    settle_s: float = 3.0  # after a background task finishes, how long to wait for the CLI's follow-up turn
    semaphore: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.max_concurrent)

    def _options(self, req: TurnRequest, bridge: Any, stderr: deque[str]) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=req.cwd,
            resume=req.session_id,
            permission_mode=req.permission_mode,  # type: ignore[arg-type]
            can_use_tool=bridge,
            cli_path=self.cli_path,
            model=req.model or self.model,
            effort=req.effort,  # type: ignore[arg-type]
            # None would pass an *empty* system prompt; we want real Claude Code.
            system_prompt={"type": "preset", "preset": "claude_code"},
            # include "local" so "Always allow" rules written to
            # .claude/settings.local.json are honoured on later turns.
            setting_sources=["user", "project", "local"],
            include_partial_messages=self.stream_deltas,
            stderr=stderr.append,
        )

    async def run_turn(
        self,
        req: TurnRequest,
        sink: TurnSink,
        prompter: Prompter,
        handle: TurnHandle | None = None,
    ) -> TurnResult:
        handle = handle or TurnHandle()
        try:
            return await self._run_once(req, sink, prompter, handle)
        except (ProcessError, ResultError) as exc:
            # A vanished session surfaces as ResultError("...No conversation found
            # with session ID...") at connect time; older CLIs used ProcessError.
            stderr = f"{getattr(exc, 'stderr', '') or ''}\n{exc}"
            if req.session_id and SESSION_NOT_FOUND_RE.search(stderr):
                await sink.on_notice(
                    f"Session `{req.session_id[:8]}…` no longer exists on disk — starting a fresh session."
                )
                fresh = TurnRequest(req.thread_key, req.prompt, req.cwd, None, req.permission_mode, req.model)
                try:
                    return await self._run_once(fresh, sink, prompter, handle)
                except ClaudeSDKError as exc2:
                    return self._error_result(req, exc2)
            return self._error_result(req, exc)
        except CLINotFoundError as exc:
            return self._error_result(req, exc, f"claude CLI not found ({exc})")
        except CLIJSONDecodeError as exc:
            return self._error_result(req, exc, "could not parse CLI output (see logs)")
        except ClaudeSDKError as exc:
            return self._error_result(req, exc)

    @staticmethod
    def _error_result(req: TurnRequest, exc: Exception, text: str | None = None) -> TurnResult:
        log.exception("turn failed for %s", req.thread_key)
        stderr_tail = ""
        if isinstance(exc, ProcessError) and exc.stderr:
            stderr_tail = "\n".join(exc.stderr.strip().splitlines()[-10:])
        return TurnResult(
            session_id=req.session_id,
            subtype="error",
            is_error=True,
            result_text="",
            error=text or f"{type(exc).__name__}: {exc}",
            stderr_tail=stderr_tail,
        )

    async def _run_once(
        self, req: TurnRequest, sink: TurnSink, prompter: Prompter, handle: TurnHandle
    ) -> TurnResult:
        stderr: deque[str] = deque(maxlen=50)
        bridge = _PermissionBridge(req, sink, prompter)
        options = self._options(req, bridge, stderr)

        segment = 0
        buffers: list[str] = [""]
        live = ""  # partial text from stream deltas since last full AssistantMessage
        session_id = req.session_id
        result: TurnResult | None = None
        num_turns = 0
        duration_ms = 0
        answered = False  # a result for our own prompt (not an injected turn) has arrived
        busy = True  # a model turn is running
        background: dict[str, str] = {}  # running background tasks: task_id -> description
        seen_background: set[str] = set()
        notified = False  # a background task ended since the last turn started; the CLI may inject one
        shown: tuple[str, ...] | None = None
        deadline: float | None = None
        timed_out = False
        loop = asyncio.get_running_loop()

        async with self.semaphore:
            async with ClaudeSDKClient(options=options) as client:
                handle.client = client
                # Messages are read by a side task so waits below can time out: cancelling
                # `anext()` on the SDK stream would break it.
                queue: asyncio.Queue[Any] = asyncio.Queue()
                handle.wake = lambda: queue.put_nowait(_WAKE)
                pump = asyncio.create_task(_pump(client, queue))
                watchdog: asyncio.Task[None] | None = None
                if self.turn_timeout_s > 0:
                    watchdog = asyncio.create_task(self._watchdog(sink, handle))
                try:
                    await client.query(req.prompt)
                    while True:
                        timeout: float | None = None
                        handle.idle = answered and not busy
                        if handle.idle:
                            if handle.interrupted:
                                break
                            if background:
                                if deadline is None:
                                    deadline = loop.time() + self.background_wait_s
                                if tuple(background.values()) != shown:
                                    shown = tuple(background.values())
                                    await sink.on_background(list(shown))
                                timeout = max(0.0, deadline - loop.time())
                            elif notified:
                                timeout = self.settle_s
                            else:
                                break
                        try:
                            msg = await asyncio.wait_for(queue.get(), timeout)
                        except TimeoutError:
                            timed_out = bool(background)
                            break
                        if msg is _WAKE:
                            continue
                        if msg is _END:
                            break
                        if isinstance(msg, _PumpError):
                            if result is None:
                                raise msg.exc
                            log.warning("stream failed after a result for %s: %s", req.thread_key, msg.exc)
                            break

                        init = isinstance(msg, SystemMessage) and msg.subtype == "init"
                        if init:
                            notified = False  # this turn delivers any notification queued before it
                        main_turn = isinstance(msg, (AssistantMessage, StreamEvent)) and msg.parent_tool_use_id is None
                        if not busy and (init or main_turn):
                            busy = True
                            handle.idle = False
                            if answered:
                                shown = None
                                await sink.on_working()

                        if isinstance(msg, SystemMessage):
                            if init and msg.data.get("session_id"):
                                if result is None or msg.data["session_id"] != session_id:
                                    session_id = msg.data["session_id"]
                                    await sink.on_session_started(session_id)
                            elif msg.subtype == "background_tasks_changed":
                                running = {
                                    t["task_id"]: t.get("description") or t["task_id"]
                                    for t in msg.data.get("tasks") or []
                                    if t.get("task_id")
                                }
                                # Sent before the task's notification: expect a follow-up turn.
                                notified = notified or bool(background.keys() - running.keys())
                                background = running
                                seen_background.update(running)
                            elif isinstance(msg, TaskNotificationMessage):
                                background.pop(msg.task_id, None)
                                notified = notified or msg.task_id in seen_background
                            elif isinstance(msg, TaskUpdatedMessage) and msg.status in TERMINAL_TASK_STATUSES:
                                background.pop(msg.task_id, None)

                        elif isinstance(msg, StreamEvent):
                            if not self.stream_deltas or msg.parent_tool_use_id is not None:
                                continue
                            ev = msg.event
                            if ev.get("type") == "content_block_delta":
                                delta = ev.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    live += delta.get("text", "")
                                    await sink.on_text(segment, _join(buffers[segment], live))

                        elif isinstance(msg, AssistantMessage):
                            if msg.error:
                                await sink.on_notice(f"API error: `{msg.error}`")
                            if msg.parent_tool_use_id is not None:
                                for block in msg.content:
                                    if isinstance(block, ToolUseBlock):
                                        await sink.on_tool_use(block.id, block.name, block.input, True)
                                continue
                            texts = [b.text for b in msg.content if isinstance(b, TextBlock) and b.text.strip()]
                            if texts:
                                buffers[segment] = _join(buffers[segment], "\n\n".join(texts))
                                live = ""
                                await sink.on_text(segment, buffers[segment])
                            for block in msg.content:
                                if isinstance(block, ToolUseBlock):
                                    await sink.on_tool_use(block.id, block.name, block.input, False)
                                    if buffers[segment]:
                                        segment += 1
                                        buffers.append("")
                                        live = ""

                        elif isinstance(msg, UserMessage):
                            if isinstance(msg.content, list):
                                for block in msg.content:
                                    if isinstance(block, ToolResultBlock):
                                        await sink.on_tool_result(block.tool_use_id, bool(block.is_error))

                        elif isinstance(msg, ResultMessage):
                            busy = False
                            if msg.origin is None or msg.origin.get("kind") == "human":
                                answered = True  # otherwise an injected turn (task notification, …)
                            num_turns += msg.num_turns
                            duration_ms += msg.duration_ms
                            result = TurnResult(
                                session_id=msg.session_id or session_id,
                                subtype=msg.subtype,
                                is_error=msg.is_error,
                                result_text=msg.result or "",
                                total_cost_usd=msg.total_cost_usd,
                                duration_ms=duration_ms,
                                num_turns=num_turns,
                                error="; ".join(msg.errors) if msg.errors else None,
                                stderr_tail="\n".join(list(stderr)[-10:]) if msg.is_error else "",
                            )
                            if buffers[segment]:  # a follow-up turn starts a new message
                                segment += 1
                                buffers.append("")
                                live = ""
                finally:
                    handle.client = None
                    handle.idle = False
                    handle.wake = None
                    pump.cancel()
                    if watchdog is not None:
                        watchdog.cancel()

        if timed_out:
            await sink.on_notice(
                f"Background tasks still running after {int(self.background_wait_s)}s — stopped: "
                + ", ".join(background.values())
            )
        if result is None or not answered:
            result = TurnResult(
                session_id=session_id,
                subtype="interrupted" if handle.interrupted else "no_result",
                is_error=not handle.interrupted,
                result_text="",
                error=None if handle.interrupted else "CLI ended without a result",
                stderr_tail="\n".join(list(stderr)[-10:]),
            )
        elif handle.interrupted and result.subtype == "success":
            result.subtype = "interrupted"
        elif timed_out and result.subtype == "success":
            result.subtype = "background_timeout"
        await sink.on_result(result)
        return result

    async def _watchdog(self, sink: TurnSink, handle: TurnHandle) -> None:
        """Interrupt the turn once it has run for `turn_timeout_s` seconds."""
        await asyncio.sleep(self.turn_timeout_s)
        await sink.on_notice(f"Turn exceeded {int(self.turn_timeout_s)}s — interrupting.")
        await handle.interrupt()


async def _pump(client: ClaudeSDKClient, queue: asyncio.Queue[Any]) -> None:
    try:
        async for msg in client.receive_messages():
            queue.put_nowait(msg)
    except Exception as exc:  # noqa: BLE001 - re-raised by the consumer
        queue.put_nowait(_PumpError(exc))
    queue.put_nowait(_END)


def _join(a: str, b: str) -> str:
    if not a:
        return b
    if not b:
        return a
    return a + ("\n\n" if not a.endswith("\n") else "") + b


class _PermissionBridge:
    """Adapts the SDK's can_use_tool callback to a Prompter."""

    def __init__(self, req: TurnRequest, sink: TurnSink, prompter: Prompter) -> None:
        self.req = req
        self.sink = sink
        self.prompter = prompter

    async def __call__(
        self, tool_name: str, input_data: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name == "AskUserQuestion":
            await self.sink.on_waiting("question", tool_name)
            try:
                answer = await self.prompter.ask_question(self.req.thread_key, input_data.get("questions", []))
            finally:
                await self.sink.on_working()
            updated: dict[str, Any] = {"questions": input_data.get("questions", [])}
            if answer.response:
                updated["response"] = answer.response
            else:
                updated["answers"] = answer.answers or {}
            return PermissionResultAllow(updated_input=updated)

        await self.sink.on_waiting("permission", tool_name)
        try:
            decision = await self.prompter.ask_permission(self.req.thread_key, tool_name, input_data, context)
        finally:
            await self.sink.on_working()

        if decision.kind == "deny":
            msg = "Denied by user via Slack"
            if decision.message:
                msg += f": {decision.message}"
            return PermissionResultDeny(message=msg, interrupt=False)
        if decision.kind == "always":
            persist = [s for s in (context.suggestions or []) if s.destination == "localSettings"]
            return PermissionResultAllow(updated_input=input_data, updated_permissions=persist or None)
        return PermissionResultAllow(updated_input=input_data)
