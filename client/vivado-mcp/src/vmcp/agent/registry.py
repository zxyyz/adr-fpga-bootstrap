"""Live session registry.

Sessions are deliberately *not* persisted across daemon restarts: a Tcl
interpreter's state lives in its process, so a restart can only honestly report
that the session is gone. Jobs are detached processes and *must* survive, which
is why they get their own on-disk state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from ..common.errors import BadRequest, NotFound, SessionBusy
from ..common.ids import new_id
from ..common.models import SessionInfo, SessionState
from . import paths, toolinfo
from .session.tclshell import TOOL_SPECS, TclShellSession, build_session

log = logging.getLogger(__name__)

DEFAULT_IDLE_TTL = 1800.0
_GC_INTERVAL = 60.0


class SessionRegistry:
    def __init__(self, idle_ttl: float = DEFAULT_IDLE_TTL) -> None:
        self.idle_ttl = idle_ttl
        self._sessions: dict[str, TclShellSession] = {}
        #: One writer per project path — Vivado refuses to open a locked .xpr,
        #: and two interpreters sharing a project can corrupt it.
        self._project_owner: dict[str, str] = {}
        self._open_lock = asyncio.Lock()

    async def open(
        self,
        *,
        kind: str,
        label: str,
        settings_sh: str,
        cwd: str | None,
        boot_timeout: float = 180.0,
    ) -> SessionInfo:
        if kind not in TOOL_SPECS:
            raise BadRequest(
                f"tool kind {kind!r} has no Tcl shell; known: {sorted(TOOL_SPECS)}"
            )
        workdir = Path(cwd).expanduser() if cwd else paths.HOME
        if not workdir.is_dir():
            raise BadRequest(f"cwd is not a directory: {workdir}")

        exe, env = await toolinfo.resolve(settings_sh, kind)
        version = None
        with contextlib.suppress(Exception):
            version = (await toolinfo.probe(label, settings_sh, kind)).version

        async with self._open_lock:
            session_id = new_id("s")
            session = build_session(
                kind=kind,
                label=label,
                exe=exe,
                env=env,
                cwd=workdir,
                directory=paths.session_dir(session_id),
                version=version,
                boot_timeout=boot_timeout,
            )
            await session.boot()
            self._sessions[session_id] = session
        return session.info()

    def get(self, session_id: str) -> TclShellSession:
        session = self._sessions.get(session_id)
        if session is None:
            known = ", ".join(sorted(self._sessions)) or "none"
            raise NotFound(f"no session {session_id!r}; open sessions: {known}")
        return session

    def list(self) -> list[SessionInfo]:
        return [s.info() for s in self._sessions.values()]

    async def close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            raise NotFound(f"no session {session_id!r}")
        self.release_project(session_id)
        await session.close()

    async def close_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        self._project_owner.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.close()

    def claim_project(self, session_id: str, project: Path) -> None:
        self.get(session_id)
        normalized = str(project.resolve())
        owner = self._project_owner.get(normalized)
        if owner is not None and owner != session_id:
            raise SessionBusy(
                f"project {normalized} is already owned by session {owner}"
            )
        previous = self.project_for_session(session_id)
        if previous is not None and previous != normalized:
            raise BadRequest(
                f"session {session_id} already owns project {previous}; close it first"
            )
        self._project_owner[normalized] = session_id

    def release_project(self, session_id: str) -> str | None:
        for project, owner in list(self._project_owner.items()):
            if owner == session_id:
                self._project_owner.pop(project, None)
                return project
        return None

    def project_for_session(self, session_id: str) -> str | None:
        for project, owner in self._project_owner.items():
            if owner == session_id:
                return project
        return None

    def project_owner(self, project: Path) -> str | None:
        return self._project_owner.get(str(project.resolve()))

    async def gc_loop(self) -> None:
        """Reap dead and long-idle sessions. A Vivado process holds ~1 GB RSS."""
        while True:
            await asyncio.sleep(_GC_INTERVAL)
            for session_id, session in list(self._sessions.items()):
                reason = None
                if session.state is SessionState.DEAD:
                    reason = "process exited"
                elif session.idle_s > self.idle_ttl:
                    reason = f"idle for {session.idle_s:.0f}s"
                if reason is None:
                    continue
                log.info(
                    "reaping session %s (%s): %s", session_id, session.label, reason
                )
                self._sessions.pop(session_id, None)
                self.release_project(session_id)
                with contextlib.suppress(Exception):
                    await session.close()
