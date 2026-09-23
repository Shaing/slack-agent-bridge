"""Throttled Slack output: coalesced chat.update, per-channel pacing, overflow."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from .render import md_to_mrkdwn, split_for_slack, summarize_tool_input
from .runner import TurnResult

log = logging.getLogger(__name__)

MAX_TOOL_LINES = 30


class ChannelGate:
    """Enforces a minimum interval between writes to the same channel."""

    def __init__(self, min_interval: float = 1.2) -> None:
        self.min_interval = min_interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, channel: str) -> None:
        lock = self._locks.setdefault(channel, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            wait = self._last.get(channel, 0.0) + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[channel] = time.monotonic()


class ThrottledMessage:
    """One Slack message whose text is edited in place, at most every `interval` s."""

    def __init__(
        self,
        client: AsyncWebClient,
        channel: str,
        thread_ts: str,
        gate: ChannelGate,
        interval: float,
    ) -> None:
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.gate = gate
        self.interval = interval
        self.ts: str | None = None
        self._desired = ""
        self._sent = ""
        self._last_update = 0.0
        self._flusher: asyncio.Task[None] | None = None
        self._posting: asyncio.Lock = asyncio.Lock()

    async def post(self, text: str, **kwargs: Any) -> str | None:
        async with self._posting:
            if self.ts is not None:
                self.set_text(text)
                return self.ts
            self._desired = text
            await self.gate.wait(self.channel)
            try:
                resp = await self.client.chat_postMessage(
                    channel=self.channel,
                    thread_ts=self.thread_ts,
                    text=text,
                    unfurl_links=False,
                    unfurl_media=False,
                    **kwargs,
                )
                self.ts = resp["ts"]
                self._sent = text
                self._last_update = time.monotonic()
            except SlackApiError as exc:
                log.error("postMessage failed: %s", exc.response.get("error"))
            return self.ts

    def set_text(self, text: str) -> None:
        self._desired = text
        if self.ts is None:
            return
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._run_flusher())

    async def _run_flusher(self) -> None:
        while self._desired != self._sent:
            wait = self._last_update + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            await self._update()

    async def _update(self) -> None:
        if self.ts is None or self._desired == self._sent:
            return
        text = self._desired
        await self.gate.wait(self.channel)
        try:
            await self.client.chat_update(channel=self.channel, ts=self.ts, text=text)
            self._sent = text
        except SlackApiError as exc:
            log.error("chat.update failed: %s", exc.response.get("error"))
            self._sent = text  # avoid hammering on a permanent failure
        self._last_update = time.monotonic()

    async def flush(self) -> None:
        if self._flusher and not self._flusher.done():
            self._flusher.cancel()
            try:
                await self._flusher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self.ts is None and self._desired:
            await self.post(self._desired)
        else:
            await self._update()


class TurnOutput:
    """TurnSink that renders one turn into a Slack thread.

    Message 1: status header + tool activity (edited in place).
    Message 2..n: assistant prose, one message per segment (+ overflow).
    """

    def __init__(
        self,
        client: AsyncWebClient,
        channel: str,
        thread_ts: str,
        *,
        cwd: str,
        gate: ChannelGate,
        edit_interval: float,
        max_chars: int,
        show_tools: bool = True,
        user_ts: str | None = None,
    ) -> None:
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.cwd = cwd
        self.gate = gate
        self.edit_interval = edit_interval
        self.max_chars = max_chars
        self.show_tools = show_tools
        self.user_ts = user_ts

        self.status = ThrottledMessage(client, channel, thread_ts, gate, edit_interval)
        self.header = ":hourglass_flowing_sand: Working…"
        self.tool_lines: list[str] = []
        self.tool_index: dict[str, int] = {}
        self.segments: dict[int, list[ThrottledMessage]] = {}
        self.segment_text: dict[int, str] = {}
        self.session_id: str | None = None
        self.result: TurnResult | None = None

    # -- lifecycle ---------------------------------------------------------- #
    async def start(self) -> str | None:
        await self._react("eyes")
        return await self.status.post(self._render_status())

    async def _react(self, name: str, remove: bool = False) -> None:
        if not self.user_ts:
            return
        try:
            if remove:
                await self.client.reactions_remove(channel=self.channel, timestamp=self.user_ts, name=name)
            else:
                await self.client.reactions_add(channel=self.channel, timestamp=self.user_ts, name=name)
        except SlackApiError as exc:
            if exc.response.get("error") not in ("already_reacted", "no_reaction"):
                log.debug("reaction %s failed: %s", name, exc.response.get("error"))

    def _render_status(self) -> str:
        lines = [self.header]
        if self.show_tools and self.tool_lines:
            shown = self.tool_lines[-MAX_TOOL_LINES:]
            hidden = len(self.tool_lines) - len(shown)
            if hidden:
                lines.append(f"_… {hidden} earlier tool call{'s' if hidden > 1 else ''}_")
            lines.extend(shown)
        text = "\n".join(lines)
        return text if len(text) <= self.max_chars else text[: self.max_chars - 1] + "…"

    def _set_header(self, header: str) -> None:
        self.header = header
        self.status.set_text(self._render_status())

    # -- TurnSink ----------------------------------------------------------- #
    async def on_session_started(self, session_id: str) -> None:
        self.session_id = session_id

    async def on_text(self, segment: int, text: str) -> None:
        self.segment_text[segment] = text
        chunks = split_for_slack(md_to_mrkdwn(text), self.max_chars)
        msgs = self.segments.setdefault(segment, [])
        for i, chunk in enumerate(chunks):
            if i < len(msgs):
                msgs[i].set_text(chunk)
            else:
                msg = ThrottledMessage(self.client, self.channel, self.thread_ts, self.gate, self.edit_interval)
                msgs.append(msg)
                await msg.post(chunk)

    async def on_tool_use(self, tool_use_id: str, name: str, inp: dict[str, Any], subagent: bool) -> None:
        line = summarize_tool_input(name, inp, self.cwd)
        if subagent:
            line = "    ↳ " + line
        self.tool_index[tool_use_id] = len(self.tool_lines)
        self.tool_lines.append(line)
        self.status.set_text(self._render_status())

    async def on_tool_result(self, tool_use_id: str, is_error: bool) -> None:
        idx = self.tool_index.get(tool_use_id)
        if is_error and idx is not None and not self.tool_lines[idx].endswith(":x:"):
            self.tool_lines[idx] += "  :x:"
            self.status.set_text(self._render_status())

    async def on_waiting(self, kind: str, tool_name: str) -> None:
        if kind == "question":
            self._set_header(":question: Waiting for your answer…")
        else:
            self._set_header(f":lock: Waiting for your approval — *{tool_name}*")
        await self.status.flush()

    async def on_working(self) -> None:
        self._set_header(":hourglass_flowing_sand: Working…")

    async def on_notice(self, text: str) -> None:
        await self.gate.wait(self.channel)
        try:
            await self.client.chat_postMessage(
                channel=self.channel, thread_ts=self.thread_ts, text=f":information_source: {text}"
            )
        except SlackApiError as exc:
            log.error("notice failed: %s", exc.response.get("error"))

    async def on_result(self, result: TurnResult) -> None:
        self.result = result
        if result.subtype == "interrupted":
            header = ":octagonal_sign: Stopped"
        elif result.is_error:
            header = f":x: Error — {result.error or result.subtype}"
        elif result.subtype != "success":
            header = f":warning: {result.subtype}"
        else:
            header = ":white_check_mark: Done"
        meta = []
        if result.num_turns:
            meta.append(f"{result.num_turns} turn{'s' if result.num_turns != 1 else ''}")
        if result.duration_ms:
            meta.append(f"{result.duration_ms / 1000:.0f}s")
        if result.total_cost_usd is not None:
            meta.append(f"${result.total_cost_usd:.2f}")
        if meta:
            header += " · " + " · ".join(meta)
        self._set_header(header)

        # Final flush of everything, in thread order.
        await self.status.flush()
        for seg in sorted(self.segments):
            for msg in self.segments[seg]:
                await msg.flush()

        # Post the result text only if it was not already rendered.
        if result.result_text and not self._rendered_endswith(result.result_text):
            for chunk in split_for_slack(md_to_mrkdwn(result.result_text), self.max_chars):
                await ThrottledMessage(self.client, self.channel, self.thread_ts, self.gate, self.edit_interval).post(chunk)
        if result.is_error and result.stderr_tail:
            await ThrottledMessage(self.client, self.channel, self.thread_ts, self.gate, self.edit_interval).post(
                f"```\n{result.stderr_tail[:2500]}\n```"
            )
        await self._react("eyes", remove=True)
        await self._react("x" if result.is_error else ("octagonal_sign" if result.subtype == "interrupted" else "white_check_mark"))

    def _rendered_endswith(self, text: str) -> bool:
        if not self.segment_text:
            return False
        last = self.segment_text[max(self.segment_text)]
        return last.strip().endswith(text.strip()[-200:]) if text.strip() else True
