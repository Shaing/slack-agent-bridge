"""Slack-agnostic driver around ClaudeSDKClient.

One `run_turn` = one Claude Code turn (a fresh CLI subprocess, resumed from a
stored session id). Output is pushed through a `TurnSink`; permission prompts
and clarifying questions are answered through a `Prompter`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

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
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

log = logging.getLogger(__name__)

SESSION_NOT_FOUND_RE = re.compile(r"no conversation found|session.*not found|could not find session", re.I)


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

    async def interrupt(self) -> bool:
        if self.client is None:
            return False
        self.interrupted = True
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
            await self.client.set_permission_mode(mode)
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

        async with self.semaphore:
            async with ClaudeSDKClient(options=options) as client:
                handle.client = client
                try:
                    await client.query(req.prompt)
                    stream = client.receive_response()
                    while True:
                        try:
                            if self.turn_timeout_s > 0:
                                msg = await asyncio.wait_for(anext(stream), timeout=self.turn_timeout_s)
                            else:
                                msg = await anext(stream)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            await sink.on_notice(f"Turn exceeded {int(self.turn_timeout_s)}s — interrupting.")
                            await handle.interrupt()
                            continue

                        if isinstance(msg, SystemMessage):
                            if msg.subtype == "init" and msg.data.get("session_id"):
                                session_id = msg.data["session_id"]
                                await sink.on_session_started(session_id)

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
                            result = TurnResult(
                                session_id=msg.session_id or session_id,
                                subtype=msg.subtype,
                                is_error=msg.is_error,
                                result_text=msg.result or "",
                                total_cost_usd=msg.total_cost_usd,
                                duration_ms=msg.duration_ms,
                                num_turns=msg.num_turns,
                                error="; ".join(msg.errors) if msg.errors else None,
                                stderr_tail="\n".join(list(stderr)[-10:]) if msg.is_error else "",
                            )
                            break
                finally:
                    handle.client = None

        if result is None:
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
        await sink.on_result(result)
        return result


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
