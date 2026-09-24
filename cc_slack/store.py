"""Persisted thread -> session mapping plus per-thread runtime state."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .runner import TurnHandle

log = logging.getLogger(__name__)


@dataclass
class ThreadRecord:
    thread_key: str  # f"{channel}:{thread_ts}"
    channel: str
    thread_ts: str
    cwd: str
    owner: str
    session_id: str | None = None
    permission_mode: str = "default"
    model: str | None = None  # None = CC_MODEL / CLI default
    created_at: float = 0.0
    last_used: float = 0.0
    turns: int = 0
    in_flight: dict[str, Any] | None = None  # {"started_at", "status_ts", "user_ts"} while a turn runs

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ThreadRecord":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class ThreadSession:
    """Runtime-only state for one thread."""

    record: ThreadRecord
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    handle: TurnHandle = field(default_factory=TurnHandle)
    pending_prompt_id: str | None = None  # a question prompt awaiting free text
    queued: int = 0

    @property
    def running(self) -> bool:
        return self.lock.locked()


class ThreadStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, ThreadRecord]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.error("could not read %s: %s (starting empty)", self.path, exc)
            return {}
        return {k: ThreadRecord.from_dict(v) for k, v in raw.items()}

    def save(self, records: dict[str, ThreadRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in records.items()}, indent=1))
        os.replace(tmp, self.path)


class SessionRegistry:
    def __init__(self, store: ThreadStore) -> None:
        self.store = store
        self.records: dict[str, ThreadRecord] = store.load()
        self.sessions: dict[str, ThreadSession] = {}

    @staticmethod
    def key(channel: str, thread_ts: str) -> str:
        return f"{channel}:{thread_ts}"

    def get(self, thread_key: str) -> ThreadSession | None:
        session = self.sessions.get(thread_key)
        if session is None and thread_key in self.records:
            session = ThreadSession(self.records[thread_key])
            self.sessions[thread_key] = session
        return session

    def create(
        self,
        channel: str,
        thread_ts: str,
        cwd: str,
        owner: str,
        *,
        permission_mode: str = "default",
        model: str | None = None,
    ) -> ThreadSession:
        key = self.key(channel, thread_ts)
        now = time.time()
        record = ThreadRecord(
            key, channel, thread_ts, cwd, owner,
            permission_mode=permission_mode, model=model, created_at=now, last_used=now,
        )
        self.records[key] = record
        session = ThreadSession(record)
        self.sessions[key] = session
        self.persist()
        return session

    def persist(self) -> None:
        try:
            self.store.save(self.records)
        except OSError as exc:
            log.error("could not persist state: %s", exc)

    def in_flight(self) -> list[ThreadRecord]:
        return [r for r in self.records.values() if r.in_flight]

    def running(self) -> list[ThreadSession]:
        return [s for s in self.sessions.values() if s.running]
