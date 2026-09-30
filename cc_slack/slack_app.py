"""Slack event/action handlers and per-thread orchestration."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from typing import Any

from slack_bolt.app.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from . import lanes
from .config import EFFORTS, ConfigError, Settings, resolve_cwd
from .permissions import PromptRegistry, SlackPrompter, parse_action_value
from .render import slack_text_to_plain
from .runner import Decision, Runner, TurnHandle, TurnRequest, TurnResult
from .store import SessionRegistry, ThreadSession
from .stream import ChannelGate, TurnOutput

log = logging.getLogger(__name__)

# Leading `cwd:… mode:… model:… lane:…` options on the message that starts a thread, any order.
PREFIX_RE = re.compile(r"^\s*(cwd|mode|model|lane|effort):[ \t]*(\S+)[ \t]*\n?", re.I)
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]*$")
IGNORED_SUBTYPES = re.compile(r".+")

HELP = """*cc-slack — Claude Code via Slack*
Send me a DM to start a new Claude Code session; reply *in the thread* to continue it.
Start a new thread with options before the prompt, in any order:
`cwd:/abs/path` · `mode:plan` · `model:sonnet`   e.g. `mode:plan model:opus refactor the parser`

Commands (in a thread or top-level):
• `!help` — this text
• `!status` — session info / running turns
• `!stop` — interrupt the running turn in this thread (or stop the background tasks it is waiting for)
• `!cwd /path` — set the working directory for a *new* thread
• `!new` — forget this thread's session (keeps cwd); next message starts fresh
• `!mode [name]` — show / set the permission mode for this thread ({modes})
• `!model [name|default]` — show / set the model for this thread (e.g. `sonnet`, `opus`, `haiku`, a full model id)
• `!effort [low|medium|high|xhigh|max|default]` — show / set the effort for this thread (from the next turn)
• `!claude [text]` — in a thread the local model answered: hand it to Claude (with the local exchange as context)
Model router: `{model_router}` — simple lookups run on `{simple_model}`/{simple_effort}; `model:` or `effort:` before the prompt skips it.
Lane router: `{router}` — `lane:local` / `lane:claude` before a new thread's prompt forces the lane.
Mode and model changes apply immediately, even to a turn that is already running.
While Claude waits for a free-text answer (*Other…*), `!commands` still work; any other message is the answer.
Default cwd: `{default_cwd}` · default mode: `{default_mode}` · default model: `{default_model}`
Allowed roots: {roots}"""


def parse_prefixes(text: str) -> tuple[dict[str, str], str]:
    """Split leading `cwd:X mode:Y model:Z` options off a message."""
    opts: dict[str, str] = {}
    while m := PREFIX_RE.match(text):
        opts[m.group(1).lower()] = m.group(2)
        text = text[m.end():]
    return opts, text.strip()


def normalize_mode(value: str) -> str:
    """Case-insensitive match against the SDK's mode names (acceptedits -> acceptEdits)."""
    names = {m.lower(): m for m in ("default", "plan", "acceptEdits", "auto", "dontAsk", "bypassPermissions")}
    return names.get(value.strip().lower(), value.strip())


def parse_command(text: str) -> tuple[str, str] | None:
    if not text.startswith("!"):
        return None
    head, _, rest = text[1:].partition(" ")
    return head.strip().lower(), rest.strip()


class Bridge:
    def __init__(self, settings: Settings, client: AsyncWebClient, runner: Runner, sessions: SessionRegistry) -> None:
        self.settings = settings
        self.client = client
        self.runner = runner
        self.sessions = sessions
        self.gate = ChannelGate()
        self.prompts = PromptRegistry()
        self.prompter = SlackPrompter(
            client,
            self.prompts,
            self.gate,
            settings.prompt_timeout_s,
            cwd_lookup=lambda key: (self.sessions.records[key].cwd if key in self.sessions.records else None),
        )
        self.bot_user_id: str | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._warned_users: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.router_log = lanes.RouterLog(settings.router_log)

    # -- lifecycle ---------------------------------------------------------- #
    async def startup(self) -> None:
        auth = await self.client.auth_test()
        self.bot_user_id = auth["user_id"]
        log.info("connected as %s (%s) in team %s", auth.get("user"), self.bot_user_id, auth.get("team"))
        for record in self.sessions.in_flight():
            status_ts = (record.in_flight or {}).get("status_ts")
            record.in_flight = None
            if status_ts:
                try:
                    await self.client.chat_update(
                        channel=record.channel,
                        ts=status_ts,
                        text=":x: Bot restarted mid-turn — please resend your last message.",
                    )
                except SlackApiError as exc:
                    log.warning("recovery update failed: %s", exc.response.get("error"))
        self.sessions.persist()

    async def shutdown(self) -> None:
        self.prompts.cancel_all("bot shutting down")
        for session in self.sessions.running():
            await session.handle.interrupt()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=10)
        self.sessions.persist()

    def spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- inbound messages --------------------------------------------------- #
    def _dedupe(self, channel: str, ts: str) -> bool:
        key = f"{channel}:{ts}"
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > 500:
            self._seen.popitem(last=False)
        return True

    async def handle_message(self, event: dict[str, Any]) -> None:
        try:
            await self._handle_message(event)
        except Exception:  # noqa: BLE001
            log.exception("error handling message")

    async def _handle_message(self, event: dict[str, Any]) -> None:
        user = event.get("user")
        channel = event.get("channel")
        ts = event.get("ts")
        if not user or not channel or not ts or event.get("bot_id") or event.get("subtype"):
            return
        if user == self.bot_user_id or not self._dedupe(channel, ts):
            return
        channel_type = event.get("channel_type") or ("im" if channel.startswith("D") else "channel")

        if user not in self.settings.allowed_users:
            log.info("ignoring message from non-allowlisted user %s (add to CC_ALLOWED_USERS to allow)", user)
            if channel_type == "im" and user not in self._warned_users:
                self._warned_users.add(user)
                await self._say(channel, None, "This bot is private.")
            return
        if channel_type != "im" and not self.settings.allow_channels:
            return

        raw_text = event.get("text") or ""
        mentioned = bool(self.bot_user_id and f"<@{self.bot_user_id}>" in raw_text)
        thread_ts = event.get("thread_ts") or ts
        thread_key = self.sessions.key(channel, thread_ts)
        session = self.sessions.get(thread_key)
        if channel_type != "im" and not mentioned and session is None:
            return  # channel chatter that is not for us

        text = slack_text_to_plain(raw_text, self.bot_user_id)

        cmd = parse_command(text)
        if cmd:
            await self._handle_command(cmd, channel, thread_ts, ts, session, user)
            return

        # Free-text answer to a pending question ("Other…"); `!commands` above are never taken as the answer.
        if session and session.pending_prompt_id:
            prompt = self.prompts.get(session.pending_prompt_id)
            session.pending_prompt_id = None
            if prompt and prompt.kind == "question":
                await self._answer_free_text(prompt, text, user)
                return
        if not text:
            return

        opts: dict[str, str] = {}
        if session is None:
            opts, text = parse_prefixes(text)
            try:
                cwd = resolve_cwd(opts["cwd"], self.settings.allowed_roots) if "cwd" in opts else self.settings.default_cwd
                mode = self._check_mode(opts["mode"]) if "mode" in opts else self.settings.default_mode
                model = self._check_model(opts["model"]) if "model" in opts else None
                effort = self._check_effort(opts["effort"]) if "effort" in opts else None
                if opts.get("lane", "claude").lower() not in ("local", "claude"):
                    raise ConfigError(f"lane `{opts['lane']}` — use `lane:local` or `lane:claude`")
            except ConfigError as exc:
                await self._say(channel, thread_ts, f":x: {exc}")
                return
            session = self.sessions.create(channel, thread_ts, cwd, user, permission_mode=mode, model=model)
            session.record.effort = effort
            if not text:
                await self._say(
                    channel,
                    thread_ts,
                    f"Thread ready — cwd `{cwd}` · mode `{mode}` · model `{model or 'default'}`. "
                    "Reply in this thread with your first prompt.",
                )
                return

        await self.dispatch(session, text, user_ts=ts, opts=opts)

    # -- lanes -------------------------------------------------------------- #
    async def dispatch(self, session: ThreadSession, text: str, *, user_ts: str, opts: dict[str, str] | None = None) -> None:
        """Pick Claude or the local model for this message (Claude unless the router says otherwise)."""
        record = session.record
        opts = opts or {}
        router = self.settings.router
        if record.lane == "local":
            # Follow-up in a local thread: it stays local only while the router still says so.
            decision = None
            if router != "off":
                decision = await lanes.classify(
                    self.settings.router_url, text, record.local_history, self.settings.router_timeout_s
                )
                self._log_route(record.thread_key, text, decision, router, followup=True)
            if decision and decision.get("lane") == "local":
                if await self.run_local_turn(session, text, user_ts=user_ts, decision=decision):
                    return
            await self.hand_off(session, text, user_ts=user_ts)
            return

        first = record.session_id is None and record.turns == 0
        forced = opts.get("lane", "").lower()
        if first and forced == "local":
            if await self.run_local_turn(session, text, user_ts=user_ts, decision=None):
                return
        elif first and not opts and router == "on":
            decision = await lanes.classify(self.settings.router_url, text, None, self.settings.router_timeout_s)
            self._log_route(record.thread_key, text, decision, router)
            if decision and decision.get("lane") == "local":
                if await self.run_local_turn(session, text, user_ts=user_ts, decision=decision):
                    return
        elif first and not opts and router == "shadow":
            self.spawn(self._shadow_route(record.thread_key, text))  # never delays the Claude turn
        note = await self._route_model(session, text, first=first, opts=opts)
        await self.run_turn(session, text, user_ts=user_ts, note=note)

    async def _route_model(self, session: ThreadSession, text: str, *, first: bool, opts: dict[str, str]) -> str:
        """Pick model/effort for this Claude turn. Returns a note for the Done header ("" = nothing to say).

        A new thread's first message is classified; "simple" puts it on CC_SIMPLE_MODEL / CC_SIMPLE_EFFORT.
        Follow-ups in such a thread are classified again with the last reply as context, and anything
        that is no longer "simple" (or a router failure) moves the thread back to the default. Never down.
        """
        mode = self.settings.model_router
        record = session.record
        if mode == "off" or record.lane == "local":
            return ""
        s = self.settings
        if first:
            if {"model", "effort", "lane"} & set(opts):
                return ""  # the user chose
            if mode == "shadow":
                self.spawn(self._shadow_model_route(session, text, None))
                return ""
            decision = await lanes.classify_model(s.router_url, text, None, s.router_timeout_s)
            self._log_model_route(record.thread_key, text, decision, mode)
            record.tier = (decision or {}).get("tier")
            if record.tier == "simple":
                record.model, record.effort, record.routed = s.simple_model, s.simple_effort, True
                self.sessions.persist()
                return f"`{s.simple_model}`/{s.simple_effort} (router: simple) · `!model default` for the usual model"
            self.sessions.persist()
            if record.tier == "heavy":
                return "router: big task — `!model fable` if it needs more"
            return ""
        if record.tier != "simple":
            return ""
        history = [{"role": "assistant", "content": record.last_answer}] if record.last_answer else None
        if mode == "shadow":
            if not record.routed:
                self.spawn(self._shadow_model_route(session, text, history))
            return ""
        if not record.routed:
            return ""
        decision = await lanes.classify_model(s.router_url, text, history, s.router_timeout_s)
        self._log_model_route(record.thread_key, text, decision, mode, followup=True)
        tier = (decision or {}).get("tier")
        if tier == "simple":
            return ""
        record.model, record.effort, record.routed, record.tier = None, None, False, tier or "standard"
        self.sessions.persist()
        why = "router unavailable" if decision is None else f"router: {tier}"
        return f"moved up to `{s.model or 'default'}` ({why})"

    async def _shadow_model_route(self, session: ThreadSession, text: str, history: list[dict[str, str]] | None) -> None:
        decision = await lanes.classify_model(self.settings.router_url, text, history, max(self.settings.router_timeout_s, 30))
        self._log_model_route(session.record.thread_key, text, decision, "shadow", followup=history is not None)
        tier = (decision or {}).get("tier")
        if tier and (history is None or tier != "simple"):
            session.record.tier = tier  # a shadow thread that grows past "simple" is not checked again
            self.sessions.persist()

    def _log_model_route(
        self, thread_key: str, text: str, decision: dict[str, Any] | None, mode: str, followup: bool = False
    ) -> None:
        log.info("model route %s mode=%s tier=%s %s", thread_key, mode, (decision or {}).get("tier", "error"),
                 (decision or {}).get("reasons"))
        self.router_log.append(
            {"kind": "model_route", "thread": thread_key, "mode": mode, "followup": followup, "text": text[:500],
             **lanes.compact(decision, "tier")}
        )

    async def _shadow_route(self, thread_key: str, text: str) -> None:
        decision = await lanes.classify(self.settings.router_url, text, None, max(self.settings.router_timeout_s, 30))
        self._log_route(thread_key, text, decision, "shadow")

    def _log_route(self, thread_key: str, text: str, decision: dict[str, Any] | None, mode: str, followup: bool = False) -> None:
        log.info("route %s mode=%s lane=%s %s", thread_key, mode, (decision or {}).get("lane", "error"), (decision or {}).get("reasons"))
        self.router_log.append(
            {"kind": "route", "thread": thread_key, "mode": mode, "followup": followup, "text": text[:500], **lanes.compact(decision)}
        )

    async def run_local_turn(
        self, session: ThreadSession, prompt: str, *, user_ts: str, decision: dict[str, Any] | None
    ) -> bool:
        """Answer with the local model. Returns False if it failed, so the caller can use Claude instead."""
        record = session.record
        model = self.settings.local_model
        if session.running:
            session.queued += 1
            await self._say(record.channel, record.thread_ts, f":inbox_tray: Queued ({session.queued} ahead).")
        async with session.lock:
            session.queued = max(0, session.queued - 1)
            out = TurnOutput(
                self.client,
                record.channel,
                record.thread_ts,
                cwd=record.cwd,
                gate=self.gate,
                edit_interval=self.settings.edit_interval_s,
                max_chars=self.settings.msg_max_chars,
                show_tools=False,
                user_ts=user_ts,
            )
            out.header = f":llama: Local `{model}` answering…"
            status_ts = await out.start()
            record.in_flight = {"started_at": time.time(), "status_ts": status_ts, "user_ts": user_ts}
            self.sessions.persist()
            messages = [*record.local_history, {"role": "user", "content": prompt}]
            log.info("local turn start %s model=%s prompt=%r", record.thread_key, model, prompt[:80])
            text, t0, last_edit = "", time.monotonic(), 0.0
            try:
                async for piece in lanes.stream_local(self.settings.ollama_url, model, messages):
                    text += piece
                    if time.monotonic() - last_edit >= self.settings.edit_interval_s:
                        last_edit = time.monotonic()
                        await out.on_text(0, text)
                if not text.strip():
                    raise RuntimeError("empty reply")
            except Exception as exc:  # noqa: BLE001
                log.warning("local turn failed %s: %s", record.thread_key, exc)
                out.done_note = "handing over to Claude"
                await out.on_result(TurnResult(None, "error", True, "", error=f"local model failed: {exc}"))
                record.in_flight = None
                self.sessions.persist()
                self.router_log.append({"kind": "turn", "thread": record.thread_key, "lane": "local", "ok": False, "error": str(exc)[:200]})
                return False
            duration_ms = round((time.monotonic() - t0) * 1000)
            await out.on_text(0, text)
            p_local = ((decision or {}).get("answers") or {}).get("lane", {}).get("probabilities", {}).get("local")
            why = f"router p={p_local:.2f}" if p_local is not None else "lane:local"
            out.done_note = f"local `{model}` · {why} · `!claude` to ask Claude instead"
            await out.on_result(TurnResult(None, "success", False, text, None, duration_ms, 0))
            record.in_flight = None
            record.lane = "local"
            record.local_history = [*messages, {"role": "assistant", "content": text}][-lanes.MAX_HISTORY:]
            self.router_log.append(
                {"kind": "turn", "thread": record.thread_key, "lane": "local", "turn": record.turns, "ok": True,
                 "duration_ms": duration_ms, "chars": len(text)}
            )
            record.turns += 1
            record.last_used = time.time()
            self.sessions.persist()
            return True

    async def hand_off(self, session: ThreadSession, text: str | None, *, user_ts: str) -> None:
        """Move a local thread to Claude, with the local exchange as context in the first prompt."""
        record = session.record
        prompt = lanes.handoff_prompt(record.local_history, self.settings.local_model, text)
        record.lane = None
        record.local_history = []
        self.sessions.persist()
        await self.run_turn(session, prompt, user_ts=user_ts)

    # -- turns -------------------------------------------------------------- #
    async def run_turn(self, session: ThreadSession, prompt: str, *, user_ts: str, note: str = "") -> None:
        record = session.record
        if session.running:
            session.queued += 1
            await self._say(record.channel, record.thread_ts, f":inbox_tray: Queued ({session.queued} ahead).")
        async with session.lock:
            session.queued = max(0, session.queued - 1)
            out = TurnOutput(
                self.client,
                record.channel,
                record.thread_ts,
                cwd=record.cwd,
                gate=self.gate,
                edit_interval=self.settings.edit_interval_s,
                max_chars=self.settings.msg_max_chars,
                show_tools=self.settings.show_tools,
                user_ts=user_ts,
            )
            out.done_note = note
            if self.runner.semaphore.locked():
                out.header = ":hourglass_flowing_sand: Waiting for a free slot…"
            status_ts = await out.start()
            record.in_flight = {"started_at": time.time(), "status_ts": status_ts, "user_ts": user_ts}
            self.sessions.persist()

            session.handle = TurnHandle()
            if record.permission_mode not in self.settings.allowed_modes:
                record.permission_mode = self.settings.default_mode
            req = TurnRequest(
                record.thread_key, prompt, record.cwd, record.session_id, record.permission_mode, record.model,
                record.effort,
            )
            log.info(
                "turn start %s cwd=%s resume=%s mode=%s model=%s effort=%s prompt=%r",
                record.thread_key, record.cwd, (record.session_id or "-")[:8], record.permission_mode,
                record.model or "-", record.effort or "-", prompt[:80],
            )
            try:
                result = await self.runner.run_turn(req, out, self.prompter, session.handle)
            except Exception as exc:  # noqa: BLE001
                log.exception("unexpected failure in turn %s", record.thread_key)
                await self._say(record.channel, record.thread_ts, f":x: Unexpected error: `{exc}`")
                result = None
            finally:
                record.in_flight = None
            if result:
                log.info(
                    "turn done %s subtype=%s turns=%s cost=%s error=%s",
                    record.thread_key, result.subtype, result.num_turns, result.total_cost_usd, result.error,
                )
                if result.session_id:
                    record.session_id = result.session_id
                if result.result_text:
                    record.last_answer = result.result_text[:800]
            if self.settings.router != "off" or self.settings.model_router != "off":
                self.router_log.append(
                    {"kind": "turn", "thread": record.thread_key, "lane": "claude", "turn": record.turns,
                     "model": record.model, "effort": record.effort, "tier": record.tier,
                     "tools": len(out.tool_lines), "num_turns": result.num_turns if result else None,
                     "cost": result.total_cost_usd if result else None,
                     "duration_ms": result.duration_ms if result else None,
                     "subtype": result.subtype if result else "exception"}
                )
            record.turns += 1
            record.last_used = time.time()
            self.sessions.persist()

    # -- commands ----------------------------------------------------------- #
    async def _handle_command(
        self,
        cmd: tuple[str, str],
        channel: str,
        thread_ts: str,
        ts: str,
        session: ThreadSession | None,
        user: str,
    ) -> None:
        name, arg = cmd
        reply_ts = thread_ts if (session or thread_ts != ts) else None  # top-level replies stay top-level
        if name == "help":
            roots = ", ".join(f"`{r}`" for r in self.settings.allowed_roots)
            await self._say(
                channel,
                reply_ts,
                HELP.format(
                    default_cwd=self.settings.default_cwd,
                    roots=roots,
                    modes=", ".join(f"`{m}`" for m in self.settings.allowed_modes),
                    default_mode=self.settings.default_mode,
                    default_model=self.settings.model or "CLI default",
                    router=self.settings.router,
                    model_router=self.settings.model_router,
                    simple_model=self.settings.simple_model,
                    simple_effort=self.settings.simple_effort,
                ),
            )
        elif name == "status":
            await self._say(channel, reply_ts, self._status_text(session))
        elif name == "stop":
            if session and session.running:
                self.prompts.cancel_thread(session.record.thread_key, "stopped by user")
                ok = await session.handle.interrupt()
                await self._say(channel, reply_ts, ":octagonal_sign: Interrupting…" if ok else ":warning: Nothing to interrupt yet.")
            else:
                await self._say(channel, reply_ts, "Nothing is running in this thread.")
        elif name == "cwd":
            if not arg:
                await self._say(channel, reply_ts, "Usage: `!cwd /abs/path`")
                return
            try:
                cwd = resolve_cwd(arg, self.settings.allowed_roots)
            except ConfigError as exc:
                await self._say(channel, reply_ts, f":x: {exc}")
                return
            if session is None:
                self.sessions.create(channel, thread_ts, cwd, user, permission_mode=self.settings.default_mode)
                await self._say(channel, thread_ts, f"cwd set to `{cwd}` — reply in this thread with your first prompt.")
            elif session.record.session_id is None and not session.running:
                session.record.cwd = cwd
                self.sessions.persist()
                await self._say(channel, thread_ts, f"cwd set to `{cwd}`.")
            else:
                await self._say(channel, thread_ts, "This thread already has a session — start a new thread to change cwd.")
        elif name == "new":
            if session is None:
                await self._say(channel, reply_ts, "No session in this thread yet.")
            elif session.running:
                await self._say(channel, thread_ts, "A turn is running — `!stop` it first.")
            else:
                session.record.session_id = None
                self.sessions.persist()
                await self._say(channel, thread_ts, f":new: Session forgotten — the next message starts fresh in `{session.record.cwd}`.")
        elif name in ("mode", "model"):
            await self._set_mode_or_model(name, arg, channel, thread_ts, reply_ts, session)
        elif name == "effort":
            await self._set_effort(arg, channel, thread_ts, reply_ts, session)
        elif name == "claude":
            if session is None or session.record.lane != "local":
                await self._say(channel, reply_ts, "`!claude` works in a thread the local model answered; this one already goes to Claude.")
            elif session.running:
                await self._say(channel, thread_ts, "A turn is running — wait for it or `!stop` it first.")
            else:
                self.router_log.append({"kind": "override", "thread": session.record.thread_key, "to": "claude", "text": arg[:500]})
                await self.hand_off(session, arg or None, user_ts=ts)
        else:
            await self._say(channel, reply_ts, f"Unknown command `!{name}` — try `!help`.")

    def _check_mode(self, value: str) -> str:
        mode = normalize_mode(value)
        if mode not in self.settings.allowed_modes:
            allowed = ", ".join(f"`{m}`" for m in self.settings.allowed_modes)
            raise ConfigError(f"mode `{value}` is not allowed — choose one of {allowed} (see CC_ALLOWED_MODES)")
        return mode

    @staticmethod
    def _check_effort(value: str) -> str | None:
        value = value.strip().lower()
        if value in ("default", "reset", "none"):
            return None
        if value not in EFFORTS:
            raise ConfigError(f"effort `{value}` — choose one of {', '.join(f'`{e}`' for e in EFFORTS)} or `default`")
        return value

    async def _set_effort(self, arg: str, channel: str, thread_ts: str, reply_ts: str | None, session: ThreadSession | None) -> None:
        if session is None:
            await self._say(channel, reply_ts, "`!effort` works inside a thread. To start one with it, send e.g. `effort:low your prompt`.")
            return
        record = session.record
        if not arg:
            await self._say(channel, thread_ts, f"Effort: `{record.effort or 'default'}` · set with `!effort {'|'.join(EFFORTS)}|default`")
            return
        try:
            record.effort = self._check_effort(arg)
        except ConfigError as exc:
            await self._say(channel, thread_ts, f":x: {exc}")
            return
        record.routed = False
        self.sessions.persist()
        live = " — applies from the next turn" if session.running else ""
        await self._say(channel, thread_ts, f"Effort for this thread: `{record.effort or 'default'}`{live}.")

    @staticmethod
    def _check_model(value: str) -> str | None:
        value = value.strip()
        if value.lower() in ("default", "reset", "none"):
            return None
        if not MODEL_RE.match(value) or len(value) > 100:
            raise ConfigError(f"`{value}` doesn't look like a model name (try `sonnet`, `opus`, `haiku` or a full id)")
        return value

    async def _set_mode_or_model(
        self,
        name: str,
        arg: str,
        channel: str,
        thread_ts: str,
        reply_ts: str | None,
        session: ThreadSession | None,
    ) -> None:
        if session is None:
            await self._say(
                channel,
                reply_ts,
                f"`!{name}` works inside a thread. To start a new thread with it, "
                f"send e.g. `{name}:{'plan' if name == 'mode' else 'sonnet'} your prompt`.",
            )
            return
        record = session.record
        if not arg:
            if name == "mode":
                allowed = ", ".join(f"`{m}`" for m in self.settings.allowed_modes)
                text = f"Permission mode: `{record.permission_mode}` · allowed: {allowed}"
            else:
                text = f"Model: `{record.model or self.settings.model or 'default'}` · set with `!model sonnet|opus|haiku|<id>|default`"
            await self._say(channel, thread_ts, text)
            return
        value: str | None
        try:
            if name == "mode":
                value = self._check_mode(arg)
                record.permission_mode = value
            else:
                value = self._check_model(arg)
                record.model = value
                if record.routed:  # the router's effort went with its model choice
                    record.effort = None
                record.routed = False
        except ConfigError as exc:
            await self._say(channel, thread_ts, f":x: {exc}")
            return
        self.sessions.persist()
        label = "Permission mode" if name == "mode" else "Model"
        shown = value or f"{self.settings.model or 'default'} (default)"
        live = ""
        if session.running:
            if name == "mode":
                ok = await session.handle.set_permission_mode(value or record.permission_mode)
            else:
                ok = await session.handle.set_model(value or self.settings.model)
            live = " — applied to the running turn too" if ok else " — applies from the next turn"
        warn = ""
        if name == "mode" and value in ("auto", "bypassPermissions", "dontAsk"):
            warn = "\n:warning: In this mode some or all actions run *without* asking you in Slack."
        await self._say(channel, thread_ts, f"{label} for this thread: `{shown}`{live}.{warn}")

    def _status_text(self, session: ThreadSession | None) -> str:
        running = self.sessions.running()
        slots = f"{len(running)}/{self.settings.max_concurrent} slots busy"
        if session is None:
            if not self.sessions.records:
                return f"No sessions yet · {slots}"
            lines = [f"*Active threads* · {slots}"]
            recent = sorted(self.sessions.records.values(), key=lambda r: r.last_used, reverse=True)[:10]
            for r in recent:
                s = self.sessions.get(r.thread_key)
                state = "running" if s and s.running else "idle"
                lines.append(f"• `{r.cwd}` — {state}, {r.turns} turns, session `{(r.session_id or '-')[:8]}`")
            return "\n".join(lines)
        r = session.record
        pending = self.prompts.for_thread(r.thread_key)
        if pending:
            state = "waiting for your input"
        elif session.handle.idle:
            state = "answered, waiting for background tasks"
        else:
            state = "running" if session.running else "idle"
        return (
            f"*Thread status*\n• cwd: `{r.cwd}`\n• session: `{r.session_id or '(none yet)'}`\n"
            f"• mode: `{r.permission_mode}`\n• model: `{r.model or self.settings.model or 'default'}`\n"
            + f"• effort: `{r.effort or 'default'}`\n"
            + (f"• model router: `{r.tier}`{' (set model/effort)' if r.routed else ''}\n" if r.tier else "")
            + (f"• lane: local `{self.settings.local_model}` (`!claude` hands it to Claude)\n" if r.lane == "local" else "")
            + f"• router: `{self.settings.router}`\n"
            f"• state: {state}\n• turns: {r.turns}"
            + (f"\n• queued: {session.queued}" if session.queued else "")
            + f"\n• {slots}"
        )

    # -- question free text ------------------------------------------------- #
    async def _answer_free_text(self, prompt: Any, text: str, user: str) -> None:
        qi = prompt.awaiting_text_q
        prompt.awaiting_text_q = None
        if qi is None or len(prompt.questions) == 1:
            q = prompt.questions[0]["question"] if prompt.questions else None
            answer = prompt.build_answer(user)
            if q:
                answer.answers = {**(answer.answers or {}), q: text}
            else:
                answer.response = text
            self.prompts.resolve(prompt.prompt_id, answer)
            return
        prompt.selections[qi] = [text]
        if prompt.needs_submit:
            await self.prompter.rerender(prompt)
        else:
            self.prompts.resolve(prompt.prompt_id, prompt.build_answer(user))

    # -- actions ------------------------------------------------------------ #
    async def handle_perm_action(self, body: dict[str, Any], action: dict[str, Any]) -> None:
        user = body["user"]["id"]
        prompt_id = action.get("value", "")
        kind = action["action_id"].removeprefix("cc_perm_")
        prompt = self.prompts.get(prompt_id)
        if user not in self.settings.allowed_users:
            await self._ephemeral(body, "You are not authorized to decide this.")
            return
        if prompt is None or kind not in ("allow", "always", "deny"):
            await self._ephemeral(body, "This prompt was already decided or has expired.")
            return
        self.prompts.resolve(prompt_id, Decision(kind, by_user=user))  # type: ignore[arg-type]

    async def handle_question_action(self, body: dict[str, Any], action: dict[str, Any]) -> None:
        user = body["user"]["id"]
        action_id: str = action["action_id"]
        data = parse_action_value(action.get("value", ""))
        prompt = self.prompts.get(data.get("p", ""))
        if user not in self.settings.allowed_users:
            await self._ephemeral(body, "You are not authorized to answer this.")
            return
        if prompt is None or prompt.kind != "question":
            await self._ephemeral(body, "This question was already answered or has expired.")
            return

        if action_id.startswith("cc_q_pick_"):
            qi, oi = int(data["q"]), int(data["o"])
            label = prompt.questions[qi]["options"][oi]["label"]
            prompt.selections[qi] = [label]
            prompt.awaiting_text_q = None
            if prompt.needs_submit:
                await self.prompter.rerender(prompt)
            else:
                self.prompts.resolve(prompt.prompt_id, prompt.build_answer(user))
        elif action_id.startswith("cc_q_multi_"):
            qi = int(action_id.rsplit("_", 1)[1])
            options = prompt.questions[qi]["options"]
            prompt.selections[qi] = [options[int(o["value"])]["label"] for o in action.get("selected_options", [])]
        elif action_id.startswith("cc_q_other_"):
            qi = int(data["q"])
            prompt.awaiting_text_q = qi
            session = self.sessions.get(prompt.thread_key)
            if session:
                session.pending_prompt_id = prompt.prompt_id
            await self.prompter.rerender(prompt)
        elif action_id == "cc_q_submit":
            self.prompts.resolve(prompt.prompt_id, prompt.build_answer(user))

    # -- slack helpers ------------------------------------------------------ #
    async def _say(self, channel: str, thread_ts: str | None, text: str) -> None:
        await self.gate.wait(channel)
        try:
            await self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, unfurl_links=False)
        except SlackApiError as exc:
            log.error("postMessage failed: %s", exc.response.get("error"))

    async def _ephemeral(self, body: dict[str, Any], text: str) -> None:
        try:
            container = body.get("container", {})
            await self.client.chat_postEphemeral(
                channel=container.get("channel_id") or body["channel"]["id"],
                user=body["user"]["id"],
                thread_ts=container.get("thread_ts"),
                text=text,
            )
        except (SlackApiError, KeyError) as exc:
            log.debug("ephemeral failed: %s", exc)


def register_handlers(app: AsyncApp, bridge: Bridge) -> None:
    @app.event("message")
    async def on_message(event: dict[str, Any]) -> None:
        bridge.spawn(bridge.handle_message(event))

    @app.event({"type": "message", "subtype": IGNORED_SUBTYPES})  # type: ignore[dict-item]
    async def on_message_subtype(event: dict[str, Any]) -> None:
        return  # edits, deletes, joins… are ignored

    @app.event("app_mention")
    async def on_mention(event: dict[str, Any]) -> None:
        bridge.spawn(bridge.handle_message(event))

    @app.action(re.compile(r"^cc_perm_"))
    async def on_perm(ack: Any, body: dict[str, Any], action: dict[str, Any]) -> None:
        await ack()
        bridge.spawn(bridge.handle_perm_action(body, action))

    @app.action(re.compile(r"^cc_q_"))
    async def on_question(ack: Any, body: dict[str, Any], action: dict[str, Any]) -> None:
        await ack()
        bridge.spawn(bridge.handle_question_action(body, action))
