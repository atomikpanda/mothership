from __future__ import annotations

import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from mship.core.message import DecisionPayload, Message, Thread


def _new_id(now: datetime) -> str:
    """Sortable, collision-free id: a timestamp prefix + short uuid."""
    return f"{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"


class MessageStore:
    """Filesystem registry for conversation threads: one JSON file per thread."""

    def __init__(self, messages_dir: Path) -> None:
        self._dir = Path(messages_dir)

    def _path(self, thread_id: str) -> Path:
        if (not thread_id or "/" in thread_id or "\\" in thread_id
                or thread_id in (".", "..") or thread_id.startswith(".")):
            raise ValueError(f"unsafe thread id: {thread_id!r}")
        return self._dir / f"{thread_id}.json"

    def save(self, thread: Thread) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(thread.id)
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".json.tmp")
        try:
            with open(fd, "w") as f:
                f.write(thread.model_dump_json(indent=2))
            Path(tmp).replace(path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def get(self, thread_id: str) -> Thread | None:
        path = self._path(thread_id)
        if not path.is_file():
            return None
        return Thread.model_validate_json(path.read_text())

    def list(self) -> list[Thread]:
        if not self._dir.is_dir():
            return []
        threads = [Thread.model_validate_json(p.read_text()) for p in self._dir.glob("*.json")]
        return sorted(threads, key=lambda t: t.updated_at, reverse=True)

    def create_thread(self, subject: str, text: str, now: datetime, task_slug: str | None = None) -> Thread:
        tid = _new_id(now)
        thread = Thread(id=tid, subject=subject, created_at=now, updated_at=now, task_slug=task_slug)
        thread.messages.append(Message(id=_new_id(now), thread_id=tid, role="human", text=text, created_at=now))
        self.save(thread)
        return thread

    def link_spec(self, thread_id: str, spec_id: str, now: datetime | None = None) -> None:
        thread = self.get(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        thread.spec_id = spec_id
        if now is not None:
            thread.updated_at = now
        self.save(thread)

    def append(self, thread_id: str, role: Literal["human", "agent"], text: str,
               now: datetime, kind: Literal["note", "needs_you", "decision"] = "note",
               decision: DecisionPayload | None = None) -> Message:
        thread = self.get(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        msg = Message(id=_new_id(now), thread_id=thread_id, role=role, text=text,
                      created_at=now, kind=kind, decision=decision)
        thread.messages.append(msg)
        thread.updated_at = now
        self.save(thread)
        return msg

    def mark_seen(self, thread_id: str, seen_at: datetime) -> Thread:
        """Advance the operator's read cursor (monotonic — never regresses).
        Does not bump updated_at: reading is not a content change and must not
        reorder the thread list."""
        thread = self.get(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        if thread.seen_at is None or seen_at > thread.seen_at:
            thread.seen_at = seen_at
            self.save(thread)
        return thread
