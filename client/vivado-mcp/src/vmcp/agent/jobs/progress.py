"""Turn Vivado log lines into compact, monotonic progress and message events."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_COMMAND = re.compile(
    r"^Command:\s+(synth_design|opt_design|place_design|phys_opt_design|route_design|write_bitstream)\b"
)
_PHASE = re.compile(r"^(Phase\s+\d+(?:\.\d+)*(?:\s+.*?))\s*(?:\|.*)?$")
_MESSAGE = re.compile(
    r"^(CRITICAL WARNING|WARNING|ERROR|FATAL):\s*(?:\[([^]]+)\])?\s*(.*)$"
)
_SUCCESS = re.compile(
    r"^(synth_design|opt_design|place_design|phys_opt_design|route_design|write_bitstream) completed successfully"
)

_STEP_BASE = {
    "synth_design": 5,
    "opt_design": 30,
    "place_design": 42,
    "phys_opt_design": 62,
    "route_design": 70,
    "write_bitstream": 95,
}
_STEP_END = {
    "synth_design": 30,
    "opt_design": 42,
    "place_design": 62,
    "phys_opt_design": 70,
    "route_design": 95,
    "write_bitstream": 100,
}


@dataclass(slots=True)
class ParsedLine:
    events: list[dict[str, Any]]
    changes: dict[str, Any]


class VivadoProgressParser:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        state = state or {}
        self.step = state.get("step")
        self.phase = state.get("phase")
        self.percent = int(state.get("percent", 0))
        self.errors = int(state.get("errors", 0))
        self.critical_warnings = int(state.get("critical_warnings", 0))
        self.warnings = int(state.get("warnings", 0))
        self._seen_messages: set[tuple[str, str]] = set()

    def feed(self, line: str) -> ParsedLine:
        text = line.strip()
        events: list[dict[str, Any]] = []
        changed: dict[str, Any] = {}

        if match := _COMMAND.match(text):
            step = match.group(1)
            if step != self.step:
                self.step = step
                self.phase = None
                self.percent = max(self.percent, _STEP_BASE[step])
                events.append({"type": "step_begin", "message": step})
                changed.update(step=step, phase=None, percent=self.percent)

        if match := _PHASE.match(text):
            phase = match.group(1).strip()
            # Vivado prints the same phase again with a checksum when it ends.
            if "| Checksum:" not in text and phase != self.phase:
                self.phase = phase
                self.percent = max(self.percent, self._phase_percent(phase))
                events.append({"type": "phase", "message": phase})
                changed.update(phase=phase, percent=self.percent)

        if match := _MESSAGE.match(text):
            severity, message_id, message = match.groups()
            message = message[:2000]
            if severity in {"ERROR", "FATAL"}:
                self.errors += 1
                event_type = "error"
                changed["errors"] = self.errors
            elif severity == "CRITICAL WARNING":
                self.critical_warnings += 1
                event_type = "critical_warning"
                changed["critical_warnings"] = self.critical_warnings
            else:
                self.warnings += 1
                event_type = "warning"
                changed["warnings"] = self.warnings
            message_key = (severity, message_id or message[:120])
            if message_key not in self._seen_messages:
                self._seen_messages.add(message_key)
                events.append(
                    {
                        "type": event_type,
                        "message": message,
                        "message_id": message_id,
                        "severity": severity.lower().replace(" ", "_"),
                    }
                )

        if match := _SUCCESS.match(text):
            step = match.group(1)
            self.percent = max(self.percent, _STEP_END[step])
            events.append({"type": "step_end", "message": step})
            changed["percent"] = self.percent

        return ParsedLine(events=events, changes=changed)

    def _phase_percent(self, phase: str) -> int:
        if self.step is None:
            return self.percent
        base, end = _STEP_BASE[self.step], _STEP_END[self.step]
        match = re.match(r"Phase\s+(\d+)(?:\.(\d+))?", phase)
        if not match:
            return base
        major = int(match.group(1))
        # Route has ~13 major phases; place ~4; other steps are shorter.
        totals = {"route_design": 13, "place_design": 4, "opt_design": 9}
        total = totals.get(self.step, 10)
        fraction = min(major / total, 0.95)
        return min(end - 1, base + int((end - base) * fraction))


def parse_messages(
    lines: list[str], min_severity: str = "warning"
) -> list[dict[str, Any]]:
    ranks = {"warning": 1, "critical_warning": 2, "error": 3, "fatal": 4}
    threshold = ranks.get(min_severity, 1)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for line in lines:
        match = _MESSAGE.match(line.strip())
        if not match:
            continue
        severity, message_id, message = match.groups()
        normalized = severity.lower().replace(" ", "_")
        if ranks[normalized] < threshold:
            continue
        key = (normalized, message_id or message[:120])
        item = grouped.setdefault(
            key,
            {
                "id": message_id,
                "severity": normalized,
                "count": 0,
                "one_example": message,
                "first_line": line.strip()[:2000],
            },
        )
        item["count"] += 1
    return sorted(
        grouped.values(), key=lambda item: (-ranks[item["severity"]], item["id"] or "")
    )
