"""Model router: simple threads go to CC_SIMPLE_MODEL / CC_SIMPLE_EFFORT; follow-ups only move up."""
from __future__ import annotations

import asyncio
import json
from collections import deque

from test_bridge_more import IM, mk
from test_lanes import QuickRunner, log_rows, texts

from cc_slack import lanes
from cc_slack.runner import Runner, TurnRequest


def fake_tier(monkeypatch, tiers, calls=None):
    """tiers: a tier name, None (router down), or a list consumed one call at a time."""
    seq = list(tiers) if isinstance(tiers, list) else None

    async def classify_model(url, text, history, timeout_s):
        if calls is not None:
            calls.append((text, history))
        tier = seq.pop(0) if seq is not None else tiers
        if tier is None:
            return None
        return {"tier": tier, "reasons": [] if tier == "simple" else ["changes_things=0.9"], "latency_ms": 5, "model": "fake",
                "answers": {"tier": {"type": "choice", "probabilities": {"simple": 0.8, "standard": 0.1, "heavy": 0.1}, "confidence": 0.8}}}
    monkeypatch.setattr(lanes, "classify_model", classify_model)


def bridge(tmp_path, **over):
    return mk(tmp_path, QuickRunner(), router_log=tmp_path / "r.jsonl", **over)


async def test_off_never_asks(tmp_path, monkeypatch):
    calls = []
    fake_tier(monkeypatch, "simple", calls)
    b, slack, runner = bridge(tmp_path)
    await b.handle_message({**IM, "ts": "1.0", "text": "check todo list"})
    assert calls == [] and runner.requests[0].model is None and runner.requests[0].effort is None


async def test_simple_thread_gets_sonnet_high(tmp_path, monkeypatch):
    fake_tier(monkeypatch, "simple")
    b, slack, runner = bridge(tmp_path, model_router="on")
    await b.handle_message({**IM, "ts": "1.0", "text": "目前ollama-mcp 狀態如何?"})
    req = runner.requests[0]
    assert (req.model, req.effort) == ("claude-sonnet-5-5", "high")
    rec = b.sessions.records["D1:1.0"]
    assert rec.routed and rec.tier == "simple" and rec.last_answer == "claude says hi"
    assert any(t.startswith(":white_check_mark: Done") and "(router: simple)" in t for t in texts(slack))
    rows = log_rows(b)
    assert rows[0]["kind"] == "model_route" and rows[0]["tier"] == "simple" and rows[0]["p"] == {"tier": 0.8}
    assert rows[1]["kind"] == "turn" and rows[1]["model"] == "claude-sonnet-5-5" and rows[1]["effort"] == "high"


async def test_standard_heavy_and_failure_keep_default(tmp_path, monkeypatch):
    for tier in ("standard", "heavy", None):
        fake_tier(monkeypatch, tier)
        b, slack, runner = bridge(tmp_path, model_router="on", state_file=tmp_path / f"s-{tier}.json")
        await b.handle_message({**IM, "ts": "1.0", "text": "升級 claude-agent-sdk 並跑測試"})
        req = runner.requests[0]
        assert req.model is None and req.effort is None and not b.sessions.records["D1:1.0"].routed
        hint = any("`!model fable`" in t for t in texts(slack))
        assert hint == (tier == "heavy")


async def test_prefixes_skip_router(tmp_path, monkeypatch):
    calls = []
    fake_tier(monkeypatch, "simple", calls)
    b, slack, runner = bridge(tmp_path, model_router="on")
    await b.handle_message({**IM, "ts": "1.0", "text": "effort:low check todo list"})
    await b.handle_message({**IM, "ts": "2.0", "text": "model:opus check todo list"})
    assert calls == []
    assert (runner.requests[0].model, runner.requests[0].effort) == (None, "low")
    assert (runner.requests[1].model, runner.requests[1].effort) == ("opus", None)
    await b.handle_message({**IM, "ts": "3.0", "text": "effort:extreme hi"})
    assert any("choose one of" in t for t in texts(slack))


async def test_followups_only_move_up(tmp_path, monkeypatch):
    calls = []
    fake_tier(monkeypatch, ["simple", "simple", "standard"], calls)
    b, slack, runner = bridge(tmp_path, model_router="on")
    await b.handle_message({**IM, "ts": "1.0", "text": "列出目前的todo list"})
    await b.handle_message({**IM, "ts": "2.0", "thread_ts": "1.0", "text": "第三項是什麼意思？"})
    assert runner.requests[1].model == "claude-sonnet-5-5"
    assert calls[1] == ("第三項是什麼意思？", [{"role": "assistant", "content": "claude says hi"}])
    await b.handle_message({**IM, "ts": "3.0", "thread_ts": "1.0", "text": "好，幫我把它改掉並 commit"})
    assert (runner.requests[2].model, runner.requests[2].effort) == (None, None)
    rec = b.sessions.records["D1:1.0"]
    assert not rec.routed and rec.tier == "standard"
    assert any("moved up to `default` (router: standard)" in t for t in texts(slack))
    # Once moved up, the router is not asked again.
    await b.handle_message({**IM, "ts": "4.0", "thread_ts": "1.0", "text": "thanks"})
    assert len(calls) == 3


async def test_followup_router_down_moves_up(tmp_path, monkeypatch):
    fake_tier(monkeypatch, ["simple", None])
    b, slack, runner = bridge(tmp_path, model_router="on")
    await b.handle_message({**IM, "ts": "1.0", "text": "svc status"})
    await b.handle_message({**IM, "ts": "2.0", "thread_ts": "1.0", "text": "then restart it"})
    assert runner.requests[1].model is None
    assert any("router unavailable" in t for t in texts(slack))


async def test_manual_model_and_effort(tmp_path, monkeypatch):
    fake_tier(monkeypatch, "simple")
    b, slack, runner = bridge(tmp_path, model_router="on")
    await b.handle_message({**IM, "ts": "1.0", "text": "svc status"})
    await b.handle_message({**IM, "ts": "2.0", "thread_ts": "1.0", "text": "!model opus"})
    rec = b.sessions.records["D1:1.0"]
    assert (rec.model, rec.effort, rec.routed) == ("opus", None, False)
    await b.handle_message({**IM, "ts": "3.0", "thread_ts": "1.0", "text": "!effort low"})
    await b.handle_message({**IM, "ts": "4.0", "thread_ts": "1.0", "text": "!effort"})
    assert rec.effort == "low" and any("Effort: `low`" in t for t in texts(slack))
    await b.handle_message({**IM, "ts": "5.0", "thread_ts": "1.0", "text": "!status"})
    assert any("• effort: `low`" in t and "• model router: `simple`" in t for t in texts(slack))
    await b.handle_message({**IM, "ts": "6.0", "thread_ts": "1.0", "text": "go on"})
    assert (runner.requests[1].model, runner.requests[1].effort) == ("opus", "low")


async def test_shadow_logs_only(tmp_path, monkeypatch):
    calls = []
    fake_tier(monkeypatch, ["simple", "standard"], calls)
    b, slack, runner = bridge(tmp_path, model_router="shadow")
    await b.handle_message({**IM, "ts": "1.0", "text": "check todo list"})
    await asyncio.gather(*b._tasks)
    assert runner.requests[0].model is None and b.sessions.records["D1:1.0"].tier == "simple"
    await b.handle_message({**IM, "ts": "2.0", "thread_ts": "1.0", "text": "now fix item 3"})
    await asyncio.gather(*b._tasks)
    assert runner.requests[1].model is None and calls[1][1] == [{"role": "assistant", "content": "claude says hi"}]
    rec = b.sessions.records["D1:1.0"]
    assert rec.tier == "standard" and not rec.routed
    kinds = [(r["kind"], r.get("tier"), r.get("followup")) for r in log_rows(b) if r["kind"] == "model_route"]
    assert kinds == [("model_route", "simple", False), ("model_route", "standard", True)]


def test_runner_passes_effort():
    runner = Runner(cli_path=None, model="opus")
    opts = runner._options(TurnRequest("k", "p", "/tmp", None, "default", None, "high"), None, deque())
    assert opts.effort == "high" and opts.model == "opus"
    assert runner._options(TurnRequest("k", "p", "/tmp"), None, deque()).effort is None


def test_compact_tier():
    d = {"tier": "simple", "reasons": [], "latency_ms": 3, "model": "m",
         "answers": {"tier": {"type": "choice", "probabilities": {"simple": 0.7, "standard": 0.2, "heavy": 0.1}, "confidence": 0.7},
                     "read_only": {"type": "noul", "noul": 0.9}}}
    c = lanes.compact(d, "tier")
    assert c["tier"] == "simple" and c["p"] == {"tier": 0.7, "read_only": 0.9} and "lane" not in c
    assert lanes.compact(None, "tier") == {"tier": None}
    assert json.dumps(c)
