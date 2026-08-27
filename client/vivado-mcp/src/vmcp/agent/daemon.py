"""The build-server daemon.

Listens on a unix socket in the agent home.  No TCP port is ever opened: the
only way in is an ssh-spawned ``vmcp-agent attach`` that pipes bytes to this
socket, so reachability and authentication are exactly the host's ssh policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import IO, Any

from .. import PROTOCOL_VERSION, __version__
from ..common.errors import BadRequest, NotFound, SessionBusy
from ..common.models import AgentInfo, HostStatus, ToolKind
from ..common.rpc import Handler, Peer
from . import fs, paths, toolinfo
from .jobs.supervisor import JobSupervisor
from .reports import ReportService
from .registry import SessionRegistry
from .workspace import PathMapper, WorkspaceManager

log = logging.getLogger(__name__)


def _require(params: dict[str, Any], name: str) -> Any:
    if name not in params:
        raise BadRequest(f"missing required parameter {name!r}")
    return params[name]


class Daemon:  # pylint: disable=too-many-public-methods
    def __init__(self) -> None:
        self.registry = SessionRegistry()
        self.jobs = JobSupervisor()
        self.workspaces = WorkspaceManager()
        self.reports = ReportService(self.jobs, self.registry, self.workspaces)
        self.started = time.monotonic()
        self.payload_sha = payload_sha256()
        self._stop = asyncio.Event()

    # -- rpc surface -------------------------------------------------------

    def handlers(self) -> dict[str, Handler]:
        return {
            "ping": self.h_ping,
            "host_status": self.h_host_status,
            "session_open": self.h_session_open,
            "session_list": self.h_session_list,
            "session_close": self.h_session_close,
            "session_eval": self.h_session_eval,
            "job_submit": self.h_job_submit,
            "flow_submit": self.h_flow_submit,
            "job_list": self.h_job_list,
            "job_status": self.h_job_status,
            "job_events": self.h_job_events,
            "job_wait": self.h_job_wait,
            "job_cancel": self.h_job_cancel,
            "job_logs": self.h_job_logs,
            "job_messages": self.h_job_messages,
            "job_artifacts": self.h_job_artifacts,
            "report_get": self.h_report_get,
            "report_diff": self.h_report_diff,
            "workspace_prepare": self.h_workspace_prepare,
            "workspace_status": self.h_workspace_status,
            "workspace_activate": self.h_workspace_activate,
            "workspace_resolve": self.h_workspace_resolve,
            "workspace_mkdir": self.h_workspace_mkdir,
            "file_read": self.h_file_read,
            "file_grep": self.h_file_grep,
            "project_claim": self.h_project_claim,
            "project_release": self.h_project_release,
            "project_binding": self.h_project_binding,
            "project_unlock": self.h_project_unlock,
            "project_locks": self.h_project_locks,
            "shutdown": self.h_shutdown,
        }

    def _agent_info(self) -> AgentInfo:
        return AgentInfo(
            protocol=PROTOCOL_VERSION,
            version=__version__,
            payload_sha256=self.payload_sha,
            pid=os.getpid(),
            uptime_s=round(time.monotonic() - self.started, 1),
            python=sys.version.split()[0],
            home=str(paths.HOME),
            sessions=len(self.registry.list()),
            jobs=len(self.jobs.list()),
        )

    def _workspace_mapper(self, params: dict[str, Any]) -> PathMapper:
        return self.workspaces.mapper(
            _require(params, "workspace"), params.get("build", "build")
        )

    @staticmethod
    def _project_lock_paths(project: Path) -> tuple[Path, Path]:
        return Path(f"{project}.lck"), project.with_suffix(".lck")

    async def h_ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        return self._agent_info().to_dict()

    async def h_host_status(self, params: dict[str, Any]) -> dict[str, Any]:
        tools = params.get("tools") or []
        probes = await asyncio.gather(
            *(
                toolinfo.probe(
                    t.get("label", "?"),
                    t["settings_sh"],
                    t.get("kind", ToolKind.VIVADO),
                )
                for t in tools
            ),
            return_exceptions=True,
        )
        reported = []
        for spec, probe in zip(tools, probes, strict=True):
            if isinstance(probe, BaseException):
                reported.append(
                    {
                        "label": spec.get("label", "?"),
                        "exists": False,
                        "error": f"{type(probe).__name__}: {probe}",
                    }
                )
            else:
                reported.append(probe.to_dict())
        all_jobs = self.jobs.list()
        active_jobs = [
            job for job in all_jobs if job["state"] in {"queued", "starting", "running"}
        ]
        max_jobs = int(params.get("max_concurrent_jobs", 1))
        return HostStatus(
            agent=self._agent_info().to_dict(),
            facts=toolinfo.host_facts().to_dict(),
            tools=reported,
            sessions=[s.to_dict() for s in self.registry.list()],
            jobs=active_jobs,
            job_slots={
                "used": sum(
                    job["state"] in {"starting", "running"} for job in active_jobs
                ),
                "max": max_jobs,
            },
        ).to_dict()

    async def h_session_open(self, params: dict[str, Any]) -> dict[str, Any]:
        info = await self.registry.open(
            kind=params.get("kind", ToolKind.VIVADO),
            label=_require(params, "label"),
            settings_sh=_require(params, "settings_sh"),
            cwd=params.get("cwd"),
            boot_timeout=float(params.get("boot_timeout", 180.0)),
        )
        return info.to_dict()

    async def h_session_list(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.registry.list()]

    async def h_session_close(self, params: dict[str, Any]) -> dict[str, Any]:
        await self.registry.close(_require(params, "session_id"))
        return {"closed": True}

    async def h_session_eval(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.registry.get(_require(params, "session_id"))
        result = await session.eval(
            _require(params, "script"), timeout=float(params.get("timeout_s", 120.0))
        )
        return result.to_dict()

    async def h_job_submit(self, params: dict[str, Any]) -> dict[str, Any]:
        project = _require(params, "project")
        workspace = params.get("workspace")
        workspace_commit = None
        project_relative = None
        if workspace:
            build = params.get("build", "build")
            project_relative = project
            project = str(self.workspaces.mapper(workspace, build).to_remote(project))
            workspace_commit = self.workspaces.status(workspace, build).get("commit")
        return await self.jobs.submit(
            target=_require(params, "target"),
            project=project,
            run=params.get("run"),
            jobs=int(params.get("jobs", 16)),
            reset=bool(params.get("reset", False)),
            strategy=params.get("strategy"),
            idempotency_key=params.get("idempotency_key"),
            label=_require(params, "label"),
            settings_sh=_require(params, "settings_sh"),
            max_concurrent_jobs=int(params.get("max_concurrent_jobs", 1)),
            nice=int(params.get("nice", 10)),
            timeout_s=float(params.get("timeout_s", 0.0)),
            stall_timeout_s=float(params.get("stall_timeout_s", 900.0)),
            workspace=workspace,
            workspace_commit=workspace_commit,
            project_relative=project_relative,
            build=params.get("build", "build"),
        )

    async def h_flow_submit(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace = _require(params, "workspace")
        build = params.get("build", "build")
        mapper = self._workspace_mapper(params)
        sources = [str(mapper.to_remote(item)) for item in _require(params, "sources")]
        xdc = [str(mapper.to_remote(item)) for item in params.get("xdc", [])]
        commit = self.workspaces.status(workspace, build).get("commit")
        return await self.jobs.submit_flow(
            sources=sources,
            xdc=xdc,
            part=_require(params, "part"),
            top=_require(params, "top"),
            target=params.get("target", "synth"),
            build_root=str(mapper.build_path),
            idempotency_key=params.get("idempotency_key"),
            label=_require(params, "label"),
            settings_sh=_require(params, "settings_sh"),
            max_concurrent_jobs=int(params.get("max_concurrent_jobs", 1)),
            nice=int(params.get("nice", 10)),
            timeout_s=float(params.get("timeout_s", 0.0)),
            stall_timeout_s=float(params.get("stall_timeout_s", 900.0)),
            workspace=workspace,
            workspace_commit=commit,
            build=build,
        )

    async def h_job_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return self.jobs.list(int(params.get("limit", 100)))

    async def h_job_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.jobs.status(_require(params, "job_id"))

    async def h_job_events(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.jobs.events(
            _require(params, "job_id"),
            int(params.get("since_seq", 0)),
            int(params.get("limit", 200)),
        )

    async def h_job_wait(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.jobs.wait(
            _require(params, "job_id"),
            float(params.get("timeout_s", 600.0)),
            params.get("since_seq"),
        )

    async def h_job_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.jobs.cancel(_require(params, "job_id"))

    async def h_job_logs(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.jobs.logs(
            _require(params, "job_id"),
            int(params.get("tail", 100)),
            params.get("grep"),
        )

    async def h_job_messages(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.jobs.messages(
            _require(params, "job_id"), params.get("min_severity", "warning")
        )

    async def h_job_artifacts(self, params: dict[str, Any]) -> dict[str, Any]:
        result = self.jobs.artifacts(_require(params, "job_id"))
        workspace = result.pop("workspace", None)
        build = result.pop("build", "build")
        if workspace:
            mapper = self.workspaces.mapper(workspace, build)
            for artifact in result.get("artifacts", []):
                artifact["path"] = mapper.to_relative(Path(artifact["path"]))
        result["job_id"] = params["job_id"]
        return result

    async def h_report_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.reports.get(params)

    async def h_report_diff(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.reports.compare(params)

    async def h_workspace_prepare(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.workspaces.prepare(
            _require(params, "workspace"), params.get("build", "build")
        )

    async def h_workspace_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.workspaces.status(
            _require(params, "workspace"), params.get("build", "build")
        )

    async def h_workspace_activate(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.workspaces.activate(
            _require(params, "workspace"),
            _require(params, "commit"),
            params.get("build", "build"),
        )

    async def h_workspace_resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.workspaces.resolve(
            _require(params, "workspace"),
            _require(params, "path"),
            params.get("build", "build"),
            must_exist=bool(params.get("must_exist", False)),
        )

    async def h_workspace_mkdir(self, params: dict[str, Any]) -> dict[str, Any]:
        mapper = self._workspace_mapper(params)
        directory = mapper.to_remote(_require(params, "path"), allow_root=True)
        directory.mkdir(parents=True, exist_ok=True)
        return {"path": params["path"], "created": True}

    async def h_file_read(self, params: dict[str, Any]) -> dict[str, Any]:
        mapper = self._workspace_mapper(params)
        return fs.file_read(
            mapper,
            _require(params, "path"),
            int(params.get("offset", 0)),
            int(params.get("limit", 12000)),
        )

    async def h_file_grep(self, params: dict[str, Any]) -> dict[str, Any]:
        mapper = self._workspace_mapper(params)
        return fs.file_grep(
            mapper,
            _require(params, "pattern"),
            params.get("glob", "**/*"),
            int(params.get("limit", 200)),
        )

    async def h_project_claim(self, params: dict[str, Any]) -> dict[str, Any]:
        mapper = self._workspace_mapper(params)
        project = mapper.to_remote(_require(params, "project"))
        if project.suffix.lower() != ".xpr":
            raise BadRequest("project path must end in .xpr")
        self.registry.claim_project(_require(params, "session_id"), project)
        return {"claimed": True, "project": params["project"]}

    async def h_project_release(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = _require(params, "session_id")
        released = self.registry.release_project(session_id)
        return {"released": released is not None}

    async def h_project_binding(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = _require(params, "session_id")
        project = self.registry.project_for_session(session_id)
        if project is None:
            raise NotFound(f"session {session_id!r} does not own an open project")
        mapper = self._workspace_mapper(params)
        return {"session_id": session_id, "project": mapper.to_relative(Path(project))}

    async def h_project_unlock(self, params: dict[str, Any]) -> dict[str, Any]:
        mapper = self._workspace_mapper(params)
        project = mapper.to_remote(_require(params, "project"))
        owner = self.registry.project_owner(project)
        if owner:
            raise SessionBusy(f"project is open in session {owner}; close it first")
        candidates = self._project_lock_paths(project)
        removed = []
        for candidate in candidates:
            if candidate.is_file():
                candidate.unlink()
                removed.append(mapper.to_relative(candidate))
        if not removed and not project.exists():
            raise NotFound(f"project does not exist: {params['project']!r}")
        return {"project": params["project"], "removed": removed}

    async def h_project_locks(self, params: dict[str, Any]) -> dict[str, Any]:
        mapper = self._workspace_mapper(params)
        project = mapper.to_remote(_require(params, "project"))
        candidates = self._project_lock_paths(project)
        locks = [mapper.to_relative(item) for item in candidates if item.is_file()]
        return {
            "project": params["project"],
            "owner_session": self.registry.project_owner(project),
            "lock_files": locks,
            "residual_lock": bool(locks)
            and self.registry.project_owner(project) is None,
        }

    async def h_shutdown(self, _params: dict[str, Any]) -> dict[str, Any]:
        log.info("shutdown requested")
        self._stop.set()
        return {"stopping": True}

    # -- serving -----------------------------------------------------------

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = Peer(reader, writer, self.handlers(), name="client")
        try:
            await peer.serve()
        finally:
            with contextlib.suppress(Exception):
                await peer.aclose()

    async def run(self) -> int:
        paths.ensure_dirs()
        # Bind only after we hold the lock, so unlinking a stale socket is safe.
        paths.DAEMON_SOCKET.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self._on_client, path=str(paths.DAEMON_SOCKET)
        )
        paths.DAEMON_SOCKET.chmod(0o600)
        log.info(
            "vmcp-agentd %s listening on %s (pid %d, payload %s)",
            __version__,
            paths.DAEMON_SOCKET,
            os.getpid(),
            self.payload_sha[:12],
        )

        gc = asyncio.create_task(self.registry.gc_loop(), name="vmcp-gc")
        scheduler = asyncio.create_task(
            self.jobs.scheduler_loop(), name="vmcp-job-scheduler"
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)
        try:
            await self._stop.wait()
        finally:
            log.info("shutting down")
            gc.cancel()
            scheduler.cancel()
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
            await self.registry.close_all()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler
            paths.DAEMON_SOCKET.unlink(missing_ok=True)
        return 0


def payload_path() -> str | None:
    """Path to the zipapp we were launched from, if any."""
    candidate = os.environ.get("VMCP_PAYLOAD") or sys.argv[0]
    return (
        candidate if candidate.endswith(".pyz") and os.path.isfile(candidate) else None
    )


def payload_sha256() -> str:
    """Identity of the deployed agent, used for the client's version handshake."""
    path = payload_path()
    if path is None:
        return "source"
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def acquire_singleton() -> IO[str] | None:
    """Take the daemon lock. Returns None if another daemon already holds it."""
    paths.ensure_dirs()
    # Held for the process lifetime; closed by the caller on exit.
    handle = open(  # pylint: disable=consider-using-with
        paths.DAEMON_LOCK, "a+", encoding="utf-8"
    )
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.truncate(0)
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


async def serve() -> int:
    return await Daemon().run()
