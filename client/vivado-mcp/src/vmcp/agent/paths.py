"""On-disk layout of the agent home.

Everything the daemon knows lives here so a restart can rehydrate rather than
forget sessions, jobs, workspaces, or cached reports.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("VMCP_HOME", "~/.vivado-mcp")).expanduser()

DAEMON_SOCKET = HOME / "vmcp-agentd.sock"
DAEMON_LOCK = HOME / "vmcp-agentd.lock"
DAEMON_LOG = HOME / "vmcp-agentd.log"
BIN = HOME / "bin"
CACHE = HOME / "cache"
SESSIONS = HOME / "sessions"
JOBS = HOME / "jobs"
WORKSPACES = HOME / "workspaces"
REPORT_CACHE = CACHE / "reports"

_DIRS = (HOME, BIN, CACHE, REPORT_CACHE, SESSIONS, JOBS, WORKSPACES)


def ensure_dirs() -> None:
    for d in _DIRS:
        d.mkdir(parents=True, exist_ok=True)
    HOME.chmod(0o700)


def session_dir(session_id: str) -> Path:
    return SESSIONS / session_id
