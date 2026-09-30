"""Lane router: Claude by default, local qwen only when jev-local says so (or lane:local)."""
from __future__ import annotations

import asyncio
import json

from test_bridge_more import IM, mk
from test_bridge_offline import ScriptedRunner

from cc_slack import lanes
from cc_slack.runner import TurnResult


class QuickRunner(ScriptedRunner):
    """Claude stand-in without the permission prompt: one tool call, one answer."""

    async def run_turn(self, req, sink, prompter, handle=None):
        self.requests.append(req)
        await sink.on_session_started("sess-q")
        await sink.on_tool_use("t1", "Read", {"file_path": "/x"}, False)
        await sink.on_text(0, "claude says hi")
        r = TurnResult("sess-q", "success", False, "claude says hi", 0.02, 800, 2)
        await sink.on_result(r)
        return r


def fake_router(monkeypatch, lane: str | None, calls: list | None = None):
    async def classify(url, text, history, timeout_s):
        if calls is not None:
            calls.append((text, history))
        if lane is None:
            return None
        return {
            "lane": lane,
            "reasons": [] if lane == "local" else ["needs_machine=0.99"],
            "answers": {"lane": {"type": "choice", "probabilities": {"local": 0.9 if lane == "local" else 0.1}, "confidence": 0.9}},
            "latency_ms": 5,
            "model": "fake",
        }
    monkeypatch.setattr(lanes, "classify", classify)


def fake_local(monkeypatch, reply: str | None = "local says hi", seen: list | None = None):
    async def stream_local(url, model, messages, timeout_s=300):
        if seen is not None:
            seen.append(messages)
        if reply is None:
            raise RuntimeError("ollama down")
        for word in reply.split(" "):
            yield word + " "
    monkeypatch.setattr(lanes, "stream_local", stream_local)


def log_rows(b):
    path = b.settings.router_log
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def texts(slack):
    return [c.get("text") or "" for k, c in slack.calls if k in ("post", "update")]


async def test_router_off_never_asks(tmp_path, monkeypatch):
    calls = []
    fake_router(monkeypatch, "local", calls)
    b, slack, runner = mk(tmp_path, QuickRunner(), router_log=tmp_path / "r.jsonl")
    await b.handle_message({**IM, "ts": "1.0", "text": "hi"})
    assert calls == [] and len(runner.requests) == 1 and log_rows(b) == []


async def test_on_local_answers_without_claude(tmp_path, monkeypatch):
    seen = []
    fake_router(monkeypatch, "local")
    fake_local(monkeypatch, "早安！今天也加油", seen)
    b, slack, runner = mk(tmp_path, QuickRunner(), router="on", router_log=tmp_path / "r.jsonl")
    await b.handle_message({**IM, "ts": "1.0", "text": "早安"})
    rec = b.sessions.records["D1:1.0"]
    assert runner.requests == []
    assert rec.lane == "local" and rec.session_id is None and rec.turns == 1
    assert rec.local_history == [{"role": "user", "content": "早安"}, {"role": "assistant", "content": "早安！今天也加油 "}]
    assert seen[0] == [{"role": "user", "content": "早安"}]
    assert any("早安！今天也加油" in t for t in texts(slack))
    assert any(t.startswith(":white_check_mark: Done") and "local `qwen3.5:latest`" in t and "p=0.90" in t for t in texts(slack))
    kinds = [(r["kind"], r.get("lane")) for r in log_rows(b)]
    assert kinds == [("route", "local"), ("turn", "local")]


async def test_on_claude_and_router_down_both_use_claude(tmp_path, monkeypatch):
    fake_local(monkeypatch)
    for lane in ("claude", None):
        fake_router(monkeypatch, lane)
        b, slack, runner = mk(tmp_path, QuickRunner(), router="on", router_log=tmp_path / f"r-{lane}.jsonl",
                              state_file=tmp_path / f"s-{lane}.json")
        await b.handle_message({**IM, "ts": "1.0", "text": "commit and push"})
        assert len(runner.requests) == 1 and runner.requests[0].prompt == "commit and push"
        rows = log_rows(b)
        assert rows[0]["kind"] == "route" and rows[0]["lane"] == lane
        assert rows[1] == {**rows[1], "kind": "turn", "lane": "claude", "tools": 1, "num_turns": 2}


async def test_prefix_skips_router_but_lane_local_forces(tmp_path, monkeypatch):
    calls = []
    fake_router(monkeypatch, "local", calls)
    fake_local(monkeypatch)
    b, slack, runner = mk(tmp_path, QuickRunner(), router="on", router_log=tmp_path / "r.jsonl")
    await b.handle_message({**IM, "ts": "1.0", "text": "model:sonnet hi"})
    assert calls == [] and len(runner.requests) == 1

    b, slack, runner = mk(tmp_path, QuickRunner(), router="off", router_log=tmp_path / "r2.jsonl", state_file=tmp_path / "s2.json")
    await b.handle_message({**IM, "ts": "2.0", "text": "lane:local what is TCP?"})
    assert runner.requests == [] and b.sessions.records["D1:2.0"].lane == "local"
    assert any("lane:local" in t for t in texts(slack))

    await b.handle_message({**IM, "ts": "3.0", "text": "lane:gpu hi"})
    assert any("use `lane:local` or `lane:claude`" in t for t in texts(slack))


async def test_followup_handed_to_claude_with_context(tmp_path, monkeypatch):
    calls = []
    fake_router(monkeypatch, "local", calls)
    fake_local(monkeypatch, "TCP is reliable")
    b, slack, runner = mk(tmp_path, QuickRunner(), router="on", router_log=tmp_path / "r.jsonl")
    await b.handle_message({**IM, "ts": "1.0", "text": "what is TCP?"})
    fake_router(monkeypatch, "claude", calls)
    await b.handle_message({**IM, "ts": "2.0", "thread_ts": "1.0", "text": "now check my firewall rules"})
    rec = b.sessions.records["D1:1.0"]
    assert calls[1][1][0] == {"role": "user", "content": "what is TCP?"}  # router saw the local history
    prompt = runner.requests[0].prompt
    assert "what is TCP?" in prompt and "TCP is reliable" in prompt and prompt.endswith("now check my firewall rules")
    assert rec.lane is None and rec.local_history == [] and rec.session_id == "sess-q"
    # Now a Claude thread: further replies never consult the router.
    await b.handle_message({**IM, "ts": "3.0", "thread_ts": "1.0", "text": "thanks"})
    assert len(calls) == 2 and runner.requests[1].prompt == "thanks"


async def test_bang_claude_hands_over(tmp_path, monkeypatch):
    fake_router(monkeypatch, "local")
    fake_local(monkeypatch, "maybe 42")
    b, slack, runner = mk(tmp_path, QuickRunner(), router="on", router_log=tmp_path / "r.jsonl")
    await b.handle_message({**IM, "ts": "1.0", "text": "meaning of life?"})
    await b.handle_message({**IM, "ts": "2.0", "thread_ts": "1.0", "text": "!claude"})
    assert runner.requests[0].prompt.endswith("Please answer my last message yourself.")
    assert b.sessions.records["D1:1.0"].lane is None
    assert any(r["kind"] == "override" for r in log_rows(b))
    await b.handle_message({**IM, "ts": "3.0", "thread_ts": "1.0", "text": "!claude"})
    assert any("already goes to Claude" in t for t in texts(slack))


async def test_local_failure_falls_back_to_claude(tmp_path, monkeypatch):
    fake_router(monkeypatch, "local")
    fake_local(monkeypatch, None)
    b, slack, runner = mk(tmp_path, QuickRunner(), router="on", router_log=tmp_path / "r.jsonl")
    await b.handle_message({**IM, "ts": "1.0", "text": "hi"})
    assert len(runner.requests) == 1 and runner.requests[0].prompt == "hi"
    assert b.sessions.records["D1:1.0"].lane is None
    assert any("local model failed" in t for t in texts(slack))


async def test_shadow_logs_but_claude_answers(tmp_path, monkeypatch):
    fake_router(monkeypatch, "local")
    b, slack, runner = mk(tmp_path, QuickRunner(), router="shadow", router_log=tmp_path / "r.jsonl")
    await b.handle_message({**IM, "ts": "1.0", "text": "hi"})
    await asyncio.gather(*b._tasks)
    assert len(runner.requests) == 1
    rows = sorted(log_rows(b), key=lambda r: r["kind"])
    assert [r["kind"] for r in rows] == ["route", "turn"]
    assert rows[0]["mode"] == "shadow" and rows[0]["lane"] == "local" and rows[0]["p"] == {"lane": 0.9}


def test_handoff_prompt_and_compact():
    p = lanes.handoff_prompt([{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}], "m", None)
    assert "<user>\nq\n</user>" in p and "<local model>\na\n</local model>" in p
    c = lanes.compact({"lane": "claude", "reasons": ["x"], "latency_ms": 3, "model": "m", "answers": {
        "needs_machine": {"type": "noul", "noul": 0.9}, "difficulty": {"type": "score", "score": 1.5},
        "lane": {"type": "choice", "probabilities": {"local": 0.2, "claude": 0.8}, "confidence": 0.8}}})
    assert c["p"] == {"needs_machine": 0.9, "difficulty": 1.5, "lane": 0.2} and c["router_ms"] == 3
    assert lanes.compact(None) == {"lane": None}
