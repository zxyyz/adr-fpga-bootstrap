"""On-disk job state and append-only event logs."""

from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...common.jsonio import read_json, write_json
from ...common.models import TERMINAL_JOB_STATES


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class JobFiles:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.spec = directory / "spec.json"
        self.script = directory / "job.tcl"
        self.env = directory / "env.json"
        self.state = directory / "state.json"
        self.state_lock = directory / "state.lock"
        self.stdout = directory / "stdout.log"
        self.vivado_log = directory / "vivado.log"
        self.events = directory / "events.jsonl"
        self.event_lock = directory / "events.lock"
        self.artifacts = directory / "artifacts.json"

    def read_state(self) -> dict[str, Any]:
        return read_json(self.state)

    def update_state(
        self, *, unless_terminal: bool = False, **changes: Any
    ) -> dict[str, Any]:
        with self.state_lock.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self.read_state()
            if unless_terminal and state.get("state") in TERMINAL_JOB_STATES:
                return state
            state.update(changes)
            write_json(self.state, state)
            return state

    def append_event(
        self, event_type: str, message: str = "", **data: Any
    ) -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.event_lock.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            seq = self._last_seq_unlocked() + 1
            event = {
                "seq": seq,
                "ts": utc_now(),
                "type": event_type,
                "message": message,
                "data": data,
            }
            with self.events.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            return event

    def read_events(self, since_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        if not self.events.exists():
            return []
        found: list[dict[str, Any]] = []
        with self.events.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a torn final append after a hard power loss
                if int(event.get("seq", 0)) > since_seq:
                    found.append(event)
                    if len(found) >= limit:
                        break
        return found

    def last_seq(self) -> int:
        with self.event_lock.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            return self._last_seq_unlocked()

    def _last_seq_unlocked(self) -> int:
        if not self.events.exists():
            return 0
        with self.events.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            lines = fh.read().splitlines()
        for line in reversed(lines):
            try:
                return int(json.loads(line).get("seq", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return 0
