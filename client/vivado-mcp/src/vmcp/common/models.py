"""Wire schemas. Single source of truth for both sides of the link."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, cast


class SessionState(StrEnum):
    BOOTING = "booting"
    IDLE = "idle"
    BUSY = "busy"
    DEAD = "dead"


class ToolKind(StrEnum):
    VIVADO = "vivado"
    VITIS_HLS = "vitis_hls"
    VITIS = "vitis"  # Vitis Unified uses a gRPC bridge rather than Tcl.


class JobKind(StrEnum):
    VIVADO_SYNTH = "vivado_synth"
    VIVADO_IMPL = "vivado_impl"
    VIVADO_BITSTREAM = "vivado_bitstream"
    VIVADO_FLOW = "vivado_flow"


class JobState(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    OOM = "oom"
    LOST = "lost"


TERMINAL_JOB_STATES = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.TIMEOUT,
        JobState.OOM,
        JobState.LOST,
    }
)


class _Model:
    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(cast(Any, self))


@dataclass(slots=True)
class ToolInfo(_Model):
    label: str
    settings_sh: str
    kind: str
    exists: bool
    exe: str | None = None
    version: str | None = None
    error: str | None = None


@dataclass(slots=True)
class SessionInfo(_Model):
    session_id: str
    kind: str
    label: str
    state: str
    cwd: str
    pid: int | None = None
    version: str | None = None
    boot_s: float | None = None
    idle_s: float = 0.0
    commands: int = 0
    log_path: str | None = None


@dataclass(slots=True)
class EvalResult(_Model):
    rc: int
    result: str
    errorinfo: str = ""
    log: str = ""
    log_lines_dropped: int = 0
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.rc == 0


@dataclass(slots=True)
class HostFacts(_Model):
    hostname: str
    cores: int
    load1: float
    mem_total_gb: float
    mem_available_gb: float
    disk_total_gb: float
    disk_free_gb: float
    home: str


@dataclass(slots=True)
class AgentInfo(_Model):
    protocol: int
    version: str
    payload_sha256: str
    pid: int
    uptime_s: float
    python: str
    home: str
    sessions: int = 0
    jobs: int = 0


@dataclass(slots=True)
class HostStatus(_Model):
    agent: dict[str, Any]
    facts: dict[str, Any]
    tools: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    job_slots: dict[str, int] = field(default_factory=dict)
