"""Runner against a scripted CLI stream: injected turns, background tasks, !stop, deadlines.

The message orders mirror what the real CLI (2.1.28x) sent in probes on 2026-09-30.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from claude_agent_sdk import AssistantMessage, ResultError, ResultMessage, SystemMessage, TextBlock
from claude_agent_sdk.types import TaskNotificationMessage

import cc_slack.runner as runner_mod
from cc_slack.runner import Runner, TurnHandle, TurnRequest

TN = {"kind": "task-notification"}


def init():
    return SystemMessage("init", {"session_id": "s1"})


def text(t, parent=None):
    return AssistantMessage([TextBlock(t)], "m", parent_tool_use_id=parent)


def result(t, turns=1, origin=None):
    return ResultMessage("success", 10, 10, False, turns, "s1", result=t, origin=origin)


def bg(*ids):
    return SystemMessage("background_tasks_changed", {"tasks": [{"task_id": i, "description": f"task {i}"} for i in ids]})


def notif(task_id, status="completed"):
    return TaskNotificationMessage("task_notification", {}, task_id, status, "/tmp/out", "done", "u", "s1")


class FakeClient:
    """Plays `script` (messages, float = sleep, Exception = raise), then stays open like the real CLI."""

    scripts: list[list] = []
    instances: list[FakeClient] = []

    def __init__(self, options):
        self.options = options
        self.script = FakeClient.scripts.pop(0)
        self.interrupts = 0
        self.closed = False
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def query(self, prompt):
        self.prompt = prompt

    async def interrupt(self):
        self.interrupts += 1

    async def receive_messages(self):
        for item in self.script:
            if isinstance(item, float):
                await asyncio.sleep(item)
            elif isinstance(item, Exception):
                raise item
            else:
                yield item
        await asyncio.Event().wait()


class Sink:
    def __init__(self):
        self.calls: list[tuple] = []
        self.texts: dict[int, str] = {}

    def __getattr__(self, name):  # any TurnSink method not defined below is just recorded
        async def record(*args):
            self.calls.append((name, *args))
        return record

    async def on_text(self, segment, t):
        self.texts[segment] = t

    def names(self):
        return [c[0] for c in self.calls]


class Prompter:
    async def ask_permission(self, *a):
        raise AssertionError("no prompts expected")

    async def ask_question(self, *a):
        raise AssertionError("no prompts expected")


@pytest.fixture
def fake(monkeypatch):
    FakeClient.scripts, FakeClient.instances = [], []
    monkeypatch.setattr(runner_mod, "ClaudeSDKClient", FakeClient)
    return FakeClient


def turn(runner, sink, handle=None, session_id=None):
    req = TurnRequest("D1:1.0", "hi", "/tmp", session_id)
    return runner.run_turn(req, sink, Prompter(), handle or TurnHandle())


async def test_plain_turn_returns_at_its_result(fake):
    fake.scripts.append([init(), text("hello"), result("hello")])
    sink = Sink()
    t0 = time.monotonic()
    res = await asyncio.wait_for(turn(Runner(settle_s=5), sink), 2)
    assert time.monotonic() - t0 < 1  # no settle wait without background tasks
    assert res.result_text == "hello" and res.subtype == "success"
    assert sink.names().count("on_result") == 1 and fake.instances[0].closed


async def test_replayed_notification_on_resume_is_not_the_answer(fake):
    """The 2026-09-29/30 empty replies: a task killed with the previous CLI replays first."""
    fake.scripts.append([
        notif("old", "stopped"), init(), result("", turns=0, origin=TN),
        init(), text("PONG"), result("PONG"),
    ])
    sink = Sink()
    res = await asyncio.wait_for(turn(Runner(settle_s=5), sink, session_id="s1"), 2)
    assert res.result_text == "PONG" and res.num_turns == 1
    assert sink.texts == {0: "PONG"}
    assert [c for c in sink.calls if c[0] == "on_result"] == [("on_result", res)]


async def test_background_task_keeps_turn_open_until_follow_up(fake):
    fake.scripts.append([
        init(), bg("b1"), text("STARTED"), result("STARTED", turns=2),
        text("B-OK", parent="toolu_b1"),  # the background agent's own output: not the main thread
        0.05, bg(), notif("b1"), init(), text("b1 finished"), result("b1 finished", origin=TN),
    ])
    sink = Sink()
    res = await asyncio.wait_for(turn(Runner(settle_s=5), sink), 2)
    assert sink.texts == {0: "STARTED", 1: "b1 finished"}  # the follow-up is a new message
    assert ("on_background", ["task b1"]) in sink.calls
    names = sink.names()
    assert names.index("on_background") < names.index("on_working") < names.index("on_result")
    assert res.result_text == "b1 finished" and res.num_turns == 3 and res.subtype == "success"


async def test_task_ending_without_follow_up_turn_settles(fake):
    fake.scripts.append([init(), bg("b1"), text("ok"), result("ok"), 0.01, bg()])
    t0 = time.monotonic()
    res = await asyncio.wait_for(turn(Runner(settle_s=0.2), Sink()), 2)
    assert 0.2 <= time.monotonic() - t0 < 1.5 and res.subtype == "success"


async def test_stop_while_waiting_for_background_tasks(fake):
    fake.scripts.append([init(), bg("b1"), text("STARTED"), result("STARTED")])
    sink, handle = Sink(), TurnHandle()
    task = asyncio.create_task(turn(Runner(), sink, handle))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if "on_background" in sink.names():
            break
    assert handle.idle
    assert await handle.interrupt()
    res = await asyncio.wait_for(task, 2)
    assert res.subtype == "interrupted" and fake.instances[0].interrupts == 0  # woke the wait, no CLI interrupt
    assert fake.instances[0].closed and not handle.idle


async def test_background_wait_deadline_stops_tasks(fake):
    fake.scripts.append([init(), bg("b1"), text("STARTED"), result("STARTED")])
    sink = Sink()
    res = await asyncio.wait_for(turn(Runner(background_wait_s=0.1), sink), 2)
    assert res.subtype == "background_timeout"
    notices = [c[1] for c in sink.calls if c[0] == "on_notice"]
    assert notices and "still running" in notices[0] and "task b1" in notices[0]


async def test_stream_error_before_result_still_retries_fresh_session(fake):
    fake.scripts.append([ResultError("No conversation found with session ID s0")])
    fake.scripts.append([init(), text("fresh"), result("fresh")])
    sink = Sink()
    res = await asyncio.wait_for(turn(Runner(), sink, session_id="s0"), 2)
    assert res.result_text == "fresh" and fake.instances[1].options.resume is None
    assert any("no longer exists" in c[1] for c in sink.calls if c[0] == "on_notice")
