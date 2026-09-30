"""Optional lane router: Claude Code or the local qwen for a new thread.

The decision comes from jev-local (~/work/jev, POST /v1/route), which asks the local model
Jev-style typed questions and applies a Claude-by-default policy. Any failure or timeout
means Claude. The local lane answers through Ollama's chat API with no tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

LOCAL_SYSTEM = (
    "You are a helpful assistant answering in a Slack thread. Answer in the same language as the user's "
    "latest message: English gets English; Chinese gets Traditional Chinese as used in Taiwan. Be concise "
    "and use Markdown sparingly. You have no tools, files or access to the user's machine or projects. Only "
    "when a request truly needs those, say so in one sentence and suggest sending `!claude`."
)
MAX_HISTORY = 20  # messages kept per local thread


async def classify(url: str, text: str, history: list[dict[str, str]] | None, timeout_s: float) -> dict[str, Any] | None:
    """POST /v1/route. Returns the decision dict, or None on any failure (caller treats None as Claude)."""
    payload: dict[str, Any] = {"text": text}
    if history:
        payload["history"] = history
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as http:
            async with http.post(url.rstrip("/") + "/v1/route", json=payload) as resp:
                if resp.status != 200:
                    log.warning("router returned HTTP %s: %s", resp.status, (await resp.text())[:200])
                    return None
                return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        log.warning("router unavailable (%s: %s) — using Claude", type(exc).__name__, exc)
        return None


async def stream_local(
    ollama_url: str, model: str, messages: list[dict[str, str]], timeout_s: float = 300
) -> AsyncIterator[str]:
    """Stream the local model's reply (content only, thinking off)."""
    body = {
        "model": model,
        "messages": [{"role": "system", "content": LOCAL_SYSTEM}, *messages],
        "stream": True,
        "think": False,
        "keep_alive": "30m",
        # Same num_ctx as ollama-agent and jev-local, so Ollama does not reload the model.
        "options": {"num_ctx": 32768},
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as http:
        async with http.post(ollama_url.rstrip("/") + "/api/chat", json=body) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ollama HTTP {resp.status}: {(await resp.text())[:200]}")
            async for line in resp.content:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(f"ollama: {chunk['error']}")
                piece = (chunk.get("message") or {}).get("content") or ""
                if piece:
                    yield piece
                if chunk.get("done"):
                    return


def handoff_prompt(history: list[dict[str, str]], model: str, text: str | None) -> str:
    """Prompt for Claude when a local thread moves to Claude: the local exchange as context."""
    lines = [f"(Earlier in this Slack thread the local model {model} answered. That exchange, for context:)"]
    for m in history:
        who = "user" if m["role"] == "user" else "local model"
        lines.append(f"<{who}>\n{m['content']}\n</{who}>")
    lines.append("---")
    lines.append(text or "Please answer my last message yourself.")
    return "\n".join(lines)


async def classify_model(
    url: str, text: str, history: list[dict[str, str]] | None, timeout_s: float
) -> dict[str, Any] | None:
    """POST /v1/route/model -> {"tier": simple|standard|heavy, ...}, or None on any failure."""
    payload: dict[str, Any] = {"text": text}
    if history:
        payload["history"] = history
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as http:
            async with http.post(url.rstrip("/") + "/v1/route/model", json=payload) as resp:
                if resp.status != 200:
                    log.warning("model router returned HTTP %s: %s", resp.status, (await resp.text())[:200])
                    return None
                return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        log.warning("model router unavailable (%s: %s)", type(exc).__name__, exc)
        return None


def compact(decision: dict[str, Any] | None, key: str = "lane") -> dict[str, Any]:
    """The part of a router decision worth logging: its verdict (`key`), reasons, one number per question."""
    if not decision:
        return {key: None}
    probs: dict[str, float] = {}
    for name, a in (decision.get("answers") or {}).items():
        if a.get("type") == "noul":
            probs[name] = a["noul"]
        elif a.get("type") == "score":
            probs[name] = a["score"]
        elif a.get("type") == "choice":
            p = a["probabilities"]
            probs[name] = p.get("local", p.get("simple", a["confidence"]))
    return {
        key: decision.get(key),
        "reasons": decision.get("reasons"),
        "p": probs,
        "router_ms": decision.get("latency_ms"),
        "router_model": decision.get("model"),
    }


class RouterLog:
    """Append-only JSONL of routing decisions and turn outcomes, for tuning the router offline."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": round(time.time(), 3), **record}, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("router log write failed: %s", exc)
