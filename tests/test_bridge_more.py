"""Offline checks for the remaining README claims: !stop, !new, queueing, channels, restart recovery, Other…, auto mode."""
from __future__ import annotations

import asyncio

import pytest
from test_bridge_offline import FakeSlack, ScriptedRunner, make_settings

from cc_slack.config import Settings
from cc_slack.runner import Runner, TurnHandle, TurnResult
from cc_slack.slack_app import Bridge
from cc_slack.store import SessionRegistry, ThreadStore


class SlowRunner(ScriptedRunner):
    """Runs until interrupted or released; records live mode/model switches."""
    def __init__(self):
        super().__init__(); self.release = asyncio.Event(); self.switches = []
    async def run_turn(self, req, sink, prompter, handle=None):
        self.requests.append(req)
        class FakeClient:
            async def interrupt(s): self.release.set()
            async def set_permission_mode(s, m): self.switches.append(("mode", m))
            async def set_model(s, m): self.switches.append(("model", m))
        handle.client = FakeClient()
        await sink.on_session_started("sess-slow")
        await self.release.wait()
        handle.client = None
        sub = "interrupted" if handle.interrupted else "success"
        r = TurnResult("sess-slow", sub, False, "", 0.0, 10, 1); await sink.on_result(r); return r

def mk(tmp_path, runner=None, **over):
    s = make_settings(tmp_path)
    for k, v in over.items(): setattr(s, k, v)
    slack = FakeSlack(); runner = runner or ScriptedRunner()
    b = Bridge(s, slack, runner, SessionRegistry(ThreadStore(s.state_file))); b.gate.min_interval = 0
    return b, slack, runner

IM = {"type": "message", "user": "UOWNER", "channel": "D1", "channel_type": "im"}

async def test_stop_and_live_switch(tmp_path):
    b, slack, runner = mk(tmp_path, SlowRunner()); await b.startup()
    t = asyncio.create_task(b.handle_message({**IM, "ts": "1.0", "text": "long task"}))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if runner.requests: break
    sess = b.sessions.get("D1:1.0"); assert sess.running
    await b.handle_message({**IM, "ts": "1.1", "thread_ts": "1.0", "text": "!mode acceptEdits"})
    await b.handle_message({**IM, "ts": "1.2", "thread_ts": "1.0", "text": "!model haiku"})
    assert ("mode", "acceptEdits") in runner.switches and ("model", "haiku") in runner.switches
    assert "applied to the running turn too" in slack.of("post")[-1]["text"]
    # !new refused while running
    await b.handle_message({**IM, "ts": "1.3", "thread_ts": "1.0", "text": "!new"})
    assert "`!stop` it first" in slack.of("post")[-1]["text"]
    # !status shows running
    await b.handle_message({**IM, "ts": "1.4", "thread_ts": "1.0", "text": "!status"})
    assert "state: running" in slack.of("post")[-1]["text"]
    await b.handle_message({**IM, "ts": "1.5", "thread_ts": "1.0", "text": "!stop"})
    assert "Interrupting" in slack.of("post")[-1]["text"]
    await asyncio.wait_for(t, 2)
    assert any(u.get("text", "").startswith(":octagonal_sign: Stopped") for u in slack.of("update"))
    assert [r["name"] for r in slack.of("react")] == ["eyes", "octagonal_sign"]
    # !new after idle forgets session but keeps mode/model/cwd
    await b.handle_message({**IM, "ts": "1.6", "thread_ts": "1.0", "text": "!new"})
    rec = sess.record
    assert rec.session_id is None and rec.permission_mode == "acceptEdits" and rec.model == "haiku"

async def test_queue_when_running(tmp_path):
    b, slack, runner = mk(tmp_path, SlowRunner()); await b.startup()
    t1 = asyncio.create_task(b.handle_message({**IM, "ts": "1.0", "text": "first"}))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if runner.requests: break
    t2 = asyncio.create_task(b.handle_message({**IM, "ts": "1.1", "thread_ts": "1.0", "text": "second"}))
    await asyncio.sleep(0.05)
    assert any("Queued (1 ahead)" in p["text"] for p in slack.of("post"))
    runner.release.set(); await asyncio.wait_for(t1, 2)
    # second turn starts after first finished: SlowRunner needs a fresh event
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(runner.requests) == 2: break
    assert len(runner.requests) == 2 and runner.requests[1].prompt == "second"
    runner.release.set(); await asyncio.wait_for(t2, 2)

async def test_channel_rules(tmp_path):
    b, slack, runner = mk(tmp_path, allow_channels=True); await b.startup()
    b.prompter.timeout_s = 0.05
    CH = {"type": "message", "user": "UOWNER", "channel": "C1", "channel_type": "channel"}
    # no mention, no session -> ignored
    await b.handle_message({**CH, "ts": "1.0", "text": "hello all"})
    assert runner.requests == [] and slack.of("post") == []
    # mention -> starts thread; both message + app_mention events -> deduped
    ev = {**CH, "ts": "2.0", "text": "<@UBOT> do it"}
    await asyncio.gather(b.handle_message(ev), b.handle_message({**ev, "type": "app_mention"}))
    assert len(runner.requests) == 1 and runner.requests[0].prompt == "do it"
    # thread reply without mention continues
    await b.handle_message({**CH, "ts": "2.1", "thread_ts": "2.0", "text": "more"})
    assert len(runner.requests) == 2 and runner.requests[1].session_id == "sess-1234"
    # stranger in channel: silently ignored (no 'private' reply)
    n = len(slack.of("post"))
    await b.handle_message({**CH, "user": "USTRANGER", "ts": "3.0", "text": "<@UBOT> hi"})
    assert len(slack.of("post")) == n and len(runner.requests) == 2

async def test_channels_off_ignores_channels(tmp_path):
    b, slack, runner = mk(tmp_path, allow_channels=False); await b.startup()
    await b.handle_message({"type": "app_mention", "user": "UOWNER", "channel": "C1", "ts": "1.0", "text": "<@UBOT> hi"})
    assert runner.requests == [] and slack.of("post") == []

async def test_restart_recovery(tmp_path):
    s = make_settings(tmp_path)
    reg = SessionRegistry(ThreadStore(s.state_file))
    sess = reg.create("D1", "1.0", s.default_cwd, "UOWNER"); sess.record.session_id = "old"
    sess.record.in_flight = {"status_ts": "77.7", "started_at": 0, "user_ts": "1.0"}; reg.persist()
    slack = FakeSlack(); b = Bridge(s, slack, ScriptedRunner(), SessionRegistry(ThreadStore(s.state_file)))
    await b.startup()
    upd = slack.of("update"); assert upd and upd[0]["ts"] == "77.7" and "restarted mid-turn" in upd[0]["text"]
    assert b.sessions.records["D1:1.0"].in_flight is None and b.sessions.records["D1:1.0"].session_id == "old"

async def test_auto_mode_when_allowed_and_warning(tmp_path):
    b, slack, runner = mk(tmp_path, allowed_modes=("default", "plan", "acceptEdits", "auto"), default_mode="auto"); await b.startup()
    b.prompter.timeout_s = 0.05
    await b.handle_message({**IM, "ts": "1.0", "text": "go"})
    assert runner.requests[0].permission_mode == "auto"
    await b.handle_message({**IM, "ts": "2.0", "text": "mode:plan hi"})
    assert runner.requests[1].permission_mode == "plan"
    await b.handle_message({**IM, "ts": "2.1", "thread_ts": "2.0", "text": "!mode auto"})
    assert ":warning:" in slack.of("post")[-1]["text"]

async def test_free_text_other_answer(tmp_path):
    b, slack, runner = mk(tmp_path); await b.startup()
    sess = b.sessions.create("D1", "1.0", b.settings.default_cwd, "UOWNER")
    qs = [{"header": "Lang", "question": "Which language?", "options": [{"label": "py"}, {"label": "go"}]}]
    task = asyncio.create_task(b.prompter.ask_question("D1:1.0", qs))
    for _ in range(100):
        await asyncio.sleep(0.01)
        p = b.prompts.for_thread("D1:1.0")
        if p and p.message_ts: break
    body = {"user": {"id": "UOWNER"}, "container": {"channel_id": "D1", "thread_ts": "1.0"}}
    await b.handle_question_action(body, {"action_id": "cc_q_other_0", "value": '{"p": "%s", "q": 0}' % p.prompt_id})
    assert sess.pending_prompt_id == p.prompt_id
    await b.handle_message({**IM, "ts": "1.1", "thread_ts": "1.0", "text": "rust"})
    ans = await asyncio.wait_for(task, 2)
    assert ans.answers == {"Which language?": "rust"} and runner.requests == []

async def test_commands_bypass_pending_free_text(tmp_path):
    b, slack, runner = mk(tmp_path); await b.startup()
    sess = b.sessions.create("D1", "1.0", b.settings.default_cwd, "UOWNER")
    qs = [{"header": "Lang", "question": "Which language?", "options": [{"label": "py"}]}]
    task = asyncio.create_task(b.prompter.ask_question("D1:1.0", qs))
    for _ in range(100):
        await asyncio.sleep(0.01)
        p = b.prompts.for_thread("D1:1.0")
        if p and p.message_ts: break
    body = {"user": {"id": "UOWNER"}, "container": {"channel_id": "D1", "thread_ts": "1.0"}}
    await b.handle_question_action(body, {"action_id": "cc_q_other_0", "value": '{"p": "%s", "q": 0}' % p.prompt_id})
    await b.handle_message({**IM, "ts": "1.1", "thread_ts": "1.0", "text": "!status"})
    assert "waiting for your input" in slack.of("post")[-1]["text"]
    assert sess.pending_prompt_id == p.prompt_id and not task.done()  # still waiting for the real answer
    await b.handle_message({**IM, "ts": "1.2", "thread_ts": "1.0", "text": "rust"})
    assert (await asyncio.wait_for(task, 2)).answers == {"Which language?": "rust"}


async def test_turn_watchdog_interrupts_without_breaking_stream():
    """CC_TURN_TIMEOUT_S > 0: the watchdog fires once and the running turn is interrupted."""
    runner = Runner(turn_timeout_s=0.05)
    notices, calls = [], []

    class Sink:
        async def on_notice(self, text): notices.append(text)

    class Client:
        async def interrupt(self): calls.append("interrupt")

    handle = TurnHandle(client=Client())
    wd = asyncio.create_task(runner._watchdog(Sink(), handle))
    await asyncio.wait_for(wd, 1)
    assert calls == ["interrupt"] and handle.interrupted and "exceeded" in notices[0]
    # a turn that finishes early cancels its watchdog without side effects
    handle2 = TurnHandle(client=Client()); calls.clear()
    wd2 = asyncio.create_task(runner._watchdog(Sink(), handle2)); await asyncio.sleep(0); wd2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wd2
    assert calls == [] and not handle2.interrupted


async def test_help_mentions_defaults_and_status_toplevel(tmp_path):
    b, slack, runner = mk(tmp_path); await b.startup()
    await b.handle_message({**IM, "ts": "1.0", "text": "!status"})
    assert "No sessions yet" in slack.of("post")[-1]["text"]
    b.sessions.create("D1", "2.0", b.settings.default_cwd, "UOWNER")
    await b.handle_message({**IM, "ts": "3.0", "text": "!status"})
    assert "Active threads" in slack.of("post")[-1]["text"]
    await b.handle_message({**IM, "ts": "4.0", "text": "!help"})
    h = slack.of("post")[-1]["text"]; assert "default mode: `default`" in h and "`!model" in h
    await b.handle_message({**IM, "ts": "5.0", "text": "!bogus"})
    assert "Unknown command" in slack.of("post")[-1]["text"]

def test_settings_rejects_bad_mode_config(tmp_path, monkeypatch):
    for k, v in {"SLACK_BOT_TOKEN": "x", "SLACK_APP_TOKEN": "y", "CC_ALLOWED_USERS": "U1",
                 "CC_DEFAULT_CWD": str(tmp_path), "CC_ALLOWED_MODES": "default,plan", "CC_DEFAULT_MODE": "auto"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CC_CLI_PATH", raising=False)
    from cc_slack.config import ConfigError
    with pytest.raises(ConfigError): Settings.from_env()
    monkeypatch.setenv("CC_ALLOWED_MODES", "default,yolo"); monkeypatch.setenv("CC_DEFAULT_MODE", "default")
    with pytest.raises(ConfigError): Settings.from_env()

def test_secret_scrub():
    import cc_slack.__main__ as m
    assert set(m.SECRET_ENV) == {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"}
