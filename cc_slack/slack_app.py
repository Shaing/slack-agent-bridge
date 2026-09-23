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

from .config import ConfigError, Settings, resolve_cwd
from .permissions import PromptRegistry, SlackPrompter, parse_action_value
from .render import slack_text_to_plain
from .runner import Decision, Runner, TurnHandle, TurnRequest
from .store import SessionRegistry, ThreadSession
from .stream import ChannelGate, TurnOutput

log = logging.getLogger(__name__)

CWD_PREFIX_RE = re.compile(r"^\s*cwd:\s*(\S+)\s*\n?", re.I)
VALID_MODES = ("default", "plan", "acceptEdits")
IGNORED_SUBTYPES = re.compile(r".+")

HELP = """*cc-slack — Claude Code via Slack*
Send me a DM to start a new Claude Code session; reply *in the thread* to continue it.
Start a message with `cwd:/abs/path` to pick the working directory for a new session.

Commands (in a thread or top-level):
• `!help` — this text
• `!status` — session info / running turns
• `!stop` — interrupt the running turn in this thread
• `!cwd /path` — set the working directory for a *new* thread
• `!new` — forget this thread's session (keeps cwd); next message starts fresh
• `!mode default|plan|acceptEdits` — permission mode for later turns in this thread
Default cwd: `{default_cwd}`
Allowed roots: {roots}"""


def parse_cwd_prefix(text: str) -> tuple[str | None, str]:
    m = CWD_PREFIX_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():].strip()


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

        # Free-text answer to a pending question ("Other…")
        if session and session.pending_prompt_id:
            prompt = self.prompts.get(session.pending_prompt_id)
            session.pending_prompt_id = None
            if prompt and prompt.kind == "question":
                await self._answer_free_text(prompt, text, user)
                return

        cmd = parse_command(text)
        if cmd:
            await self._handle_command(cmd, channel, thread_ts, ts, session, user)
            return
        if not text:
            return

        if session is None:
            cwd_arg, text = parse_cwd_prefix(text)
            try:
                cwd = resolve_cwd(cwd_arg, self.settings.allowed_roots) if cwd_arg else self.settings.default_cwd
            except ConfigError as exc:
                await self._say(channel, thread_ts, f":x: {exc}")
                return
            if not text:
                self.sessions.create(channel, thread_ts, cwd, user)
                await self._say(channel, thread_ts, f"cwd set to `{cwd}` — reply in this thread with your first prompt.")
                return
            session = self.sessions.create(channel, thread_ts, cwd, user)

        await self.run_turn(session, text, user_ts=ts)

    # -- turns -------------------------------------------------------------- #
    async def run_turn(self, session: ThreadSession, prompt: str, *, user_ts: str) -> None:
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
            if self.runner.semaphore.locked():
                out.header = ":hourglass_flowing_sand: Waiting for a free slot…"
            status_ts = await out.start()
            record.in_flight = {"started_at": time.time(), "status_ts": status_ts, "user_ts": user_ts}
            self.sessions.persist()

            session.handle = TurnHandle()
            req = TurnRequest(record.thread_key, prompt, record.cwd, record.session_id, record.permission_mode)
            log.info(
                "turn start %s cwd=%s resume=%s mode=%s prompt=%r",
                record.thread_key, record.cwd, (record.session_id or "-")[:8], record.permission_mode, prompt[:80],
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
            await self._say(channel, reply_ts, HELP.format(default_cwd=self.settings.default_cwd, roots=roots))
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
                self.sessions.create(channel, thread_ts, cwd, user)
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
                await self._say(channel, thread_ts, ":new: Session forgotten — the next message starts fresh in `%s`." % session.record.cwd)
        elif name == "mode":
            if arg not in VALID_MODES:
                await self._say(channel, reply_ts, f"Usage: `!mode {'|'.join(VALID_MODES)}`")
            elif session is None:
                await self._say(channel, reply_ts, "Use `!mode` inside a thread.")
            else:
                session.record.permission_mode = arg
                self.sessions.persist()
                await self._say(channel, thread_ts, f"Permission mode for this thread: `{arg}` (applies from the next turn).")
        else:
            await self._say(channel, reply_ts, f"Unknown command `!{name}` — try `!help`.")

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
        state = "waiting for your input" if pending else ("running" if session.running else "idle")
        return (
            f"*Thread status*\n• cwd: `{r.cwd}`\n• session: `{r.session_id or '(none yet)'}`\n"
            f"• mode: `{r.permission_mode}`\n• state: {state}\n• turns: {r.turns}"
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

    @app.event({"type": "message", "subtype": IGNORED_SUBTYPES})
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
