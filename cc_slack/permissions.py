"""Pending permission / question prompts and their Slack rendering."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from claude_agent_sdk.types import PermissionUpdate, ToolPermissionContext
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from .render import permission_blocks, question_blocks, resolved_blocks, summarize_tool_input
from .runner import Decision, QuestionAnswer
from .stream import ChannelGate

log = logging.getLogger(__name__)


@dataclass
class PendingPrompt:
    prompt_id: str
    thread_key: str
    kind: Literal["permission", "question"]
    channel: str
    thread_ts: str
    future: asyncio.Future[Any]
    tool_name: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    suggestions: list[PermissionUpdate] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    selections: dict[int, list[str]] = field(default_factory=dict)
    awaiting_text_q: int | None = None
    message_ts: str | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def needs_submit(self) -> bool:
        return len(self.questions) > 1 or any(q.get("multiSelect") for q in self.questions)

    def build_answer(self, by_user: str) -> QuestionAnswer:
        answers: dict[str, Any] = {}
        for qi, q in enumerate(self.questions):
            picked = self.selections.get(qi)
            if not picked:
                continue
            answers[q["question"]] = picked if q.get("multiSelect") else picked[0]
        return QuestionAnswer(answers=answers, by_user=by_user)


class PromptRegistry:
    def __init__(self) -> None:
        self._prompts: dict[str, PendingPrompt] = {}
        self._by_thread: dict[str, str] = {}

    def create(self, thread_key: str, kind: Literal["permission", "question"], **kw: Any) -> PendingPrompt:
        channel, _, thread_ts = thread_key.partition(":")
        prompt = PendingPrompt(
            prompt_id=uuid.uuid4().hex[:12],
            thread_key=thread_key,
            kind=kind,
            channel=channel,
            thread_ts=thread_ts,
            future=asyncio.get_running_loop().create_future(),
            **kw,
        )
        self._prompts[prompt.prompt_id] = prompt
        self._by_thread[thread_key] = prompt.prompt_id
        return prompt

    def get(self, prompt_id: str) -> PendingPrompt | None:
        return self._prompts.get(prompt_id)

    def for_thread(self, thread_key: str) -> PendingPrompt | None:
        pid = self._by_thread.get(thread_key)
        return self._prompts.get(pid) if pid else None

    def resolve(self, prompt_id: str, value: Decision | QuestionAnswer) -> PendingPrompt | None:
        prompt = self._prompts.get(prompt_id)
        if prompt is None:
            return None
        if not prompt.future.done():
            prompt.future.set_result(value)
        self.discard(prompt_id)
        return prompt

    def discard(self, prompt_id: str) -> None:
        prompt = self._prompts.pop(prompt_id, None)
        if prompt and self._by_thread.get(prompt.thread_key) == prompt_id:
            del self._by_thread[prompt.thread_key]

    def cancel_thread(self, thread_key: str, reason: str) -> None:
        prompt = self.for_thread(thread_key)
        if prompt:
            self._cancel(prompt, reason)

    def cancel_all(self, reason: str) -> None:
        for prompt in list(self._prompts.values()):
            self._cancel(prompt, reason)

    def _cancel(self, prompt: PendingPrompt, reason: str) -> None:
        value: Decision | QuestionAnswer
        if prompt.kind == "permission":
            value = Decision("deny", message=reason)
        else:
            value = QuestionAnswer(response=f"(no answer: {reason})")
        self.resolve(prompt.prompt_id, value)


def always_rule_text(suggestions: list[PermissionUpdate]) -> str | None:
    for s in suggestions:
        if s.destination == "localSettings" and s.type == "addRules" and s.rules:
            rule = s.rules[0]
            return f"{rule.tool_name}({rule.rule_content})" if rule.rule_content else rule.tool_name
    return None


class SlackPrompter:
    """Implements runner.Prompter by posting Block Kit prompts and awaiting a click."""

    def __init__(
        self,
        client: AsyncWebClient,
        registry: PromptRegistry,
        gate: ChannelGate,
        timeout_s: float,
        cwd_lookup: Callable[[str], str | None],
    ) -> None:
        self.client = client
        self.registry = registry
        self.gate = gate
        self.timeout_s = timeout_s
        self.cwd_lookup = cwd_lookup

    # -- Prompter ----------------------------------------------------------- #
    async def ask_permission(
        self, thread_key: str, tool_name: str, input_data: dict[str, Any], context: ToolPermissionContext
    ) -> Decision:
        prompt = self.registry.create(
            thread_key,
            "permission",
            tool_name=tool_name,
            input_data=input_data,
            suggestions=list(context.suggestions or []),
        )
        cwd = self.cwd_lookup(thread_key)
        blocks = permission_blocks(
            prompt.prompt_id,
            tool_name,
            input_data,
            cwd=cwd,
            title=context.title or context.display_name,
            description=context.description or context.decision_reason,
            always_rule=always_rule_text(prompt.suggestions),
        )
        summary = summarize_tool_input(tool_name, input_data, cwd)
        await self._post(prompt, blocks, f"Permission needed: {tool_name}")
        decision = await self._await(prompt, Decision("deny", message=f"no response within {int(self.timeout_s)}s"))
        label = {"allow": "Allowed once", "always": "Always allowed", "deny": "Denied"}[decision.kind]
        by = f" by <@{decision.by_user}>" if decision.by_user else ""
        note = f" — {decision.message}" if decision.message and decision.kind == "deny" else ""
        await self.mark(prompt, f"{label}{by}{note} · {summary}")
        return decision

    async def ask_question(self, thread_key: str, questions: list[dict[str, Any]]) -> QuestionAnswer:
        prompt = self.registry.create(thread_key, "question", tool_name="AskUserQuestion", questions=questions)
        await self._post(prompt, question_blocks(prompt.prompt_id, questions), "Claude has a question")
        answer = await self._await(prompt, QuestionAnswer(response=f"(no answer within {int(self.timeout_s)}s)"))
        if answer.response:
            summary = answer.response
        else:
            parts = []
            for q, a in (answer.answers or {}).items():
                parts.append(f"*{q}* → {', '.join(a) if isinstance(a, list) else a}")
            summary = "\n".join(parts) or "(no answer)"
        by = f" by <@{answer.by_user}>" if answer.by_user else ""
        await self.mark(prompt, f":speech_balloon: Answered{by}\n{summary}")
        return answer

    # -- helpers ------------------------------------------------------------ #
    async def _post(self, prompt: PendingPrompt, blocks: list[dict[str, Any]], fallback: str) -> None:
        await self.gate.wait(prompt.channel)
        try:
            resp = await self.client.chat_postMessage(
                channel=prompt.channel, thread_ts=prompt.thread_ts, blocks=blocks, text=fallback
            )
            prompt.message_ts = resp["ts"]
        except SlackApiError as exc:
            log.error("could not post prompt: %s", exc.response.get("error"))

    async def _await(self, prompt: PendingPrompt, on_timeout: Any) -> Any:
        try:
            if self.timeout_s > 0:
                return await asyncio.wait_for(asyncio.shield(prompt.future), self.timeout_s)
            return await prompt.future
        except asyncio.TimeoutError:
            self.registry.discard(prompt.prompt_id)
            await self.mark(prompt, ":hourglass: Expired — no response")
            return on_timeout
        except asyncio.CancelledError:
            # The CLI withdrew the request (e.g. after !stop) or we are shutting down.
            self.registry.discard(prompt.prompt_id)
            await self.mark(prompt, ":octagonal_sign: Cancelled")
            raise

    async def rerender(self, prompt: PendingPrompt) -> None:
        if prompt.message_ts is None:
            return
        blocks = question_blocks(prompt.prompt_id, prompt.questions, prompt.selections)
        if prompt.awaiting_text_q is not None:
            q = prompt.questions[prompt.awaiting_text_q]
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f":pencil: Reply in this thread with your answer for *{q.get('header', 'this question')}*."}
                    ],
                }
            )
        await self.gate.wait(prompt.channel)
        try:
            await self.client.chat_update(
                channel=prompt.channel, ts=prompt.message_ts, blocks=blocks, text="Claude has a question"
            )
        except SlackApiError as exc:
            log.error("could not rerender prompt: %s", exc.response.get("error"))

    async def mark(self, prompt: PendingPrompt, text: str) -> None:
        if prompt.message_ts is None:
            return
        await self.gate.wait(prompt.channel)
        try:
            await self.client.chat_update(
                channel=prompt.channel, ts=prompt.message_ts, blocks=resolved_blocks(text), text=text
            )
        except SlackApiError as exc:
            log.error("could not update prompt: %s", exc.response.get("error"))


def parse_action_value(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {"p": value}
    except (json.JSONDecodeError, TypeError):
        return {"p": value}
