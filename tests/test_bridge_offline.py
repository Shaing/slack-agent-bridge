"""Offline end-to-end: fake Slack client + scripted runner through Bridge."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from cc_slack.config import Settings
from cc_slack.runner import TurnResult
from cc_slack.slack_app import Bridge
from cc_slack.store import SessionRegistry, ThreadStore


class FakeSlack:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._ts = 0

    def _next(self) -> str:
        self._ts += 1
        return f"{1000 + self._ts}.000"

    async def auth_test(self):
        return {"user_id": "UBOT", "user": "bot", "team": "T"}

    async def chat_postMessage(self, **kw):
        self.calls.append(("post", kw))
        return {"ts": self._next()}

    async def chat_update(self, **kw):
        self.calls.append(("update", kw))
        return {"ok": True}

    async def chat_postEphemeral(self, **kw):
        self.calls.append(("ephemeral", kw))
        return {"ok": True}

    async def reactions_add(self, **kw):
        self.calls.append(("react", kw))

    async def reactions_remove(self, **kw):
        self.calls.append(("unreact", kw))

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]


class ScriptedRunner:
    """Pretends to be Runner: emits text, asks one permission, finishes."""

    def __init__(self) -> None:
        self.semaphore = asyncio.Semaphore(3)
        self.requests = []

    async def run_turn(self, req, sink, prompter, handle=None):
        self.requests.append(req)
        await sink.on_session_started("sess-1234")
        await sink.on_text(0, "Let me **check**.")
        await sink.on_tool_use("t1", "Bash", {"command": "rm -rf build"}, False)
        ctx = SimpleNamespace(suggestions=[], title=None, display_name=None, description=None, decision_reason=None)
        decision = await prompter.ask_permission(req.thread_key, "Bash", {"command": "rm -rf build"}, ctx)
        await sink.on_tool_result("t1", decision.kind == "deny")
        await sink.on_text(1, f"Decision was {decision.kind}.")
        result = TurnResult("sess-1234", "success", False, f"Decision was {decision.kind}.", 0.01, 1500, 2)
        await sink.on_result(result)
        return result


def make_settings(tmp_path) -> Settings:
    return Settings(
        slack_bot_token="xoxb", slack_app_token="xapp", allowed_users=frozenset({"UOWNER"}),
        default_cwd=str(tmp_path), allowed_roots=(os.path.realpath(str(tmp_path)),),
        state_file=tmp_path / "state.json", edit_interval_s=0.01, prompt_timeout_s=5,
    )


@pytest.fixture
def bridge(tmp_path):
    settings = make_settings(tmp_path)
    slack = FakeSlack()
    runner = ScriptedRunner()
    b = Bridge(settings, slack, runner, SessionRegistry(ThreadStore(settings.state_file)))
    b.gate.min_interval = 0
    return b, slack, runner


async def test_dm_turn_with_permission_click(bridge):
    b, slack, runner = bridge
    await b.startup()
    event = {"type": "message", "user": "UOWNER", "channel": "D1", "ts": "1.000", "text": "clean the build dir", "channel_type": "im"}

    async def clicker():
        for _ in range(100):
            await asyncio.sleep(0.02)
            prompt = b.prompts.for_thread("D1:1.000")
            if prompt and prompt.message_ts:
                body = {"user": {"id": "UOWNER"}, "container": {"channel_id": "D1", "thread_ts": "1.000"}}
                await b.handle_perm_action(body, {"action_id": "cc_perm_allow", "value": prompt.prompt_id})
                return
        raise AssertionError("prompt never appeared")

    await asyncio.gather(b.handle_message(event), clicker())

    # session persisted with id + cwd
    rec = b.sessions.records["D1:1.000"]
    assert rec.session_id == "sess-1234" and rec.turns == 1 and rec.in_flight is None
    assert runner.requests[0].cwd == b.settings.default_cwd

    posts = slack.of("post")
    texts = [p.get("text", "") for p in posts]
    assert all(p["thread_ts"] == "1.000" for p in posts)
    assert texts[0].startswith(":hourglass_flowing_sand:")           # status first
    assert "Let me *check*." in texts                                   # prose converted to mrkdwn
    assert any("blocks" in p for p in posts)                            # permission prompt with buttons
    updates = slack.of("update")
    assert any("Allowed once by <@UOWNER>" in (u.get("text") or "") for u in updates)
    assert any(u.get("text", "").startswith(":white_check_mark: Done") for u in updates)
    assert any(":wrench: *Bash*  `rm -rf build`" in u.get("text", "") for u in updates)
    assert [r["name"] for r in slack.of("react")] == ["eyes", "white_check_mark"]


async def test_second_message_resumes_and_unauthorized_ignored(bridge):
    b, slack, runner = bridge
    await b.startup()
    b.sessions.create("D1", "1.000", b.settings.default_cwd, "UOWNER").record.session_id = "old-sess"

    # Stranger DMs -> one "private" reply, nothing runs
    await b.handle_message({"type": "message", "user": "USTRANGER", "channel": "D9", "ts": "5.0", "text": "hi", "channel_type": "im"})
    await b.handle_message({"type": "message", "user": "USTRANGER", "channel": "D9", "ts": "6.0", "text": "hi", "channel_type": "im"})
    assert [p["text"] for p in slack.of("post")] == ["This bot is private."]
    assert runner.requests == []

    # Owner replies in thread: denied click by stranger is ignored, then owner denies
    event = {"type": "message", "user": "UOWNER", "channel": "D1", "ts": "2.000", "thread_ts": "1.000", "text": "go", "channel_type": "im"}

    async def clicker():
        for _ in range(100):
            await asyncio.sleep(0.02)
            prompt = b.prompts.for_thread("D1:1.000")
            if prompt and prompt.message_ts:
                stranger = {"user": {"id": "USTRANGER"}, "container": {"channel_id": "D1", "thread_ts": "1.000"}}
                await b.handle_perm_action(stranger, {"action_id": "cc_perm_allow", "value": prompt.prompt_id})
                assert b.prompts.for_thread("D1:1.000") is prompt  # still pending
                owner = {"user": {"id": "UOWNER"}, "container": {"channel_id": "D1", "thread_ts": "1.000"}}
                await b.handle_perm_action(owner, {"action_id": "cc_perm_deny", "value": prompt.prompt_id})
                return
        raise AssertionError("prompt never appeared")

    await asyncio.gather(b.handle_message(event), clicker())
    assert runner.requests[0].session_id == "old-sess"
    assert slack.of("ephemeral")[0]["user"] == "USTRANGER"
    assert any("Denied by <@UOWNER>" in (u.get("text") or "") for u in slack.of("update"))


async def test_commands(bridge):
    b, slack, runner = bridge
    await b.startup()
    im = {"type": "message", "user": "UOWNER", "channel": "D1", "channel_type": "im"}
    await b.handle_message({**im, "ts": "1.0", "text": "!help"})
    assert "cc-slack" in slack.of("post")[-1]["text"] and slack.of("post")[-1]["thread_ts"] is None
    await b.handle_message({**im, "ts": "2.0", "text": f"!cwd {b.settings.default_cwd}"})
    assert b.sessions.get("D1:2.0").record.cwd == b.settings.default_cwd
    await b.handle_message({**im, "ts": "3.0", "text": "!cwd /definitely/not/here"})
    assert slack.of("post")[-1]["text"].startswith(":x:")
    await b.handle_message({**im, "ts": "2.1", "thread_ts": "2.0", "text": "!mode plan"})
    assert b.sessions.get("D1:2.0").record.permission_mode == "plan"
    await b.handle_message({**im, "ts": "2.2", "thread_ts": "2.0", "text": "!status"})
    assert "mode: `plan`" in slack.of("post")[-1]["text"]
    assert runner.requests == []
