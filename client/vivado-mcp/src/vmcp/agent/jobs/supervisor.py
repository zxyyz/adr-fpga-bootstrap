"""Submit, inspect, wait for, cancel and rehydrate detached jobs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import time
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...common.errors import BadRequest, NotFound
from ...common.ids import new_id
from ...common.jsonio import read_json, write_json
from ...common.models import JobKind, JobState, TERMINAL_JOB_STATES
from .. import paths, toolinfo
from .progress import parse_messages
from .runner import process_matches, worker_argv
from .script_gen import generate_flow_script, generate_project_script
from .storage import JobFiles, utc_now

log = logging.getLogger(__name__)

_IMPORTANT_EVENTS = {
    "critical_warning",
    "error",
    "stalled",
    "artifact",
    "finished",
}


def _validate_submission(idempotency_key: str | None, max_concurrent_jobs: int) -> None:
    if idempotency_key is not None and (
        not idempotency_key.strip() or len(idempotency_key) > 200
    ):
        raise BadRequest("idempotency_key must be 1-200 characters")
    if max_concurrent_jobs < 1:
        raise BadRequest("max_concurrent_jobs must be at least 1")


def _create_job_files() -> tuple[str, JobFiles]:
    job_id = new_id("j")
    files = JobFiles(paths.JOBS / job_id)
    files.directory.mkdir(parents=True, mode=0o700)
    return job_id, files


def _initialize_job(
    files: JobFiles,
    *,
    spec: dict[str, Any],
    state: dict[str, Any],
    exe: str,
    environment: dict[str, str],
) -> None:
    write_json(files.spec, spec)
    write_json(
        files.env,
        {
            "exe": exe,
            "environment": environment,
            "settings_sh": spec["settings_sh"],
            "workspace": spec.get("workspace"),
            "workspace_commit": spec.get("workspace_commit"),
        },
    )
    write_json(files.state, state)
    write_json(files.artifacts, {"artifacts": [], "truncated": False})


class JobSupervisor:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

    def _files(self, job_id: str) -> JobFiles:
        if not re.fullmatch(r"j_[0-9a-f]{8}", job_id):
            raise NotFound(f"invalid job id {job_id!r}")
        files = JobFiles(paths.JOBS / job_id)
        if not files.directory.is_dir():
            raise NotFound(f"no job {job_id!r}")
        return files

    async def submit(  # pylint: disable=too-many-locals
        self,
        *,
        target: str,
        project: str,
        run: str | None,
        jobs: int,
        reset: bool,
        strategy: str | None,
        idempotency_key: str | None,
        label: str,
        settings_sh: str,
        max_concurrent_jobs: int,
        nice: int,
        timeout_s: float,
        stall_timeout_s: float,
        workspace: str | None = None,
        workspace_commit: str | None = None,
        project_relative: str | None = None,
        build: str = "build",
    ) -> dict[str, Any]:
        project_path = Path(project).expanduser().resolve()
        if project_path.suffix.lower() != ".xpr" or not project_path.is_file():
            raise BadRequest(f"project is not an existing .xpr file: {project_path}")
        _validate_submission(idempotency_key, max_concurrent_jobs)
        kind = {
            "synth": JobKind.VIVADO_SYNTH,
            "impl": JobKind.VIVADO_IMPL,
            "bitstream": JobKind.VIVADO_BITSTREAM,
            "flow": JobKind.VIVADO_FLOW,
        }.get(target)
        if kind is None:
            raise BadRequest("target must be synth, impl, bitstream, or flow")

        async with self._lock:
            replay = self._idempotent_replay(idempotency_key)
            if replay is not None:
                return replay

            exe, environment = await toolinfo.resolve(settings_sh, "vivado")
            job_id, files = _create_job_files()
            created_at = utc_now()
            created_epoch = time.time()
            report_dir = project_path.parent / ".vmcp-reports" / job_id
            script = generate_project_script(
                project=str(project_path),
                target=target,
                run=run,
                jobs=jobs,
                reset=reset,
                strategy=strategy,
                report_dir=str(report_dir),
            )
            files.script.write_text(script, encoding="utf-8")
            normalized_run = run or ("synth_1" if target == "synth" else "impl_1")
            spec = {
                "job_id": job_id,
                "kind": str(kind),
                "target": target,
                "project": str(project_path),
                "run": normalized_run,
                "cwd": str(project_path.parent),
                "jobs": jobs,
                "reset": reset,
                "strategy": strategy,
                "idempotency_key": idempotency_key,
                "label": label,
                "settings_sh": settings_sh,
                "max_concurrent_jobs": max_concurrent_jobs,
                "nice": nice,
                "timeout_s": timeout_s,
                "stall_timeout_s": stall_timeout_s,
                "created_at": created_at,
                "created_epoch": created_epoch,
                "workspace": workspace,
                "workspace_commit": workspace_commit,
                "project_relative": project_relative,
                "build": build,
                "report_dir": str(report_dir),
            }
            _initialize_job(
                files,
                spec=spec,
                state={
                    "job_id": job_id,
                    "kind": str(kind),
                    "state": str(JobState.QUEUED),
                    "target": target,
                    "project": project_relative or str(project_path),
                    "workspace": workspace,
                    "workspace_commit": workspace_commit,
                    "run": normalized_run,
                    "created_at": created_at,
                    "percent": 0,
                    "errors": 0,
                    "critical_warnings": 0,
                    "warnings": 0,
                    "tail": [],
                },
                exe=exe,
                environment=environment,
            )
            files.append_event("queued", f"{target} queued", target=target)
            self._wake.set()
        return self.status(job_id)

    async def submit_flow(  # pylint: disable=too-many-locals
        self,
        *,
        sources: list[str],
        xdc: list[str],
        part: str,
        top: str,
        target: str,
        build_root: str,
        idempotency_key: str | None,
        label: str,
        settings_sh: str,
        max_concurrent_jobs: int,
        nice: int,
        timeout_s: float,
        stall_timeout_s: float,
        workspace: str,
        workspace_commit: str | None,
        build: str = "build",
    ) -> dict[str, Any]:
        _validate_submission(idempotency_key, max_concurrent_jobs)
        source_paths = [Path(item).resolve() for item in sources]
        xdc_paths = [Path(item).resolve() for item in xdc]
        missing = [str(item) for item in source_paths + xdc_paths if not item.is_file()]
        if missing:
            raise BadRequest(f"flow input does not exist: {missing[0]}")

        async with self._lock:
            replay = self._idempotent_replay(idempotency_key)
            if replay is not None:
                return replay
            exe, environment = await toolinfo.resolve(settings_sh, "vivado")
            job_id, files = _create_job_files()
            cwd = Path(build_root).resolve() / "flows" / job_id
            cwd.mkdir(parents=True, exist_ok=False)
            marker = cwd / "flow.xpr"
            report_dir = cwd / "reports"
            created_at = utc_now()
            created_epoch = time.time()
            files.script.write_text(
                generate_flow_script(
                    sources=[str(item) for item in source_paths],
                    xdc=[str(item) for item in xdc_paths],
                    part=part,
                    top=top,
                    target=target,
                    report_dir=str(report_dir),
                ),
                encoding="utf-8",
            )
            spec = {
                "job_id": job_id,
                "kind": str(JobKind.VIVADO_FLOW),
                "target": target,
                "project": str(marker),
                "run": "flow",
                "cwd": str(cwd),
                "sources": [str(item) for item in source_paths],
                "xdc": [str(item) for item in xdc_paths],
                "part": part,
                "top": top,
                "idempotency_key": idempotency_key,
                "label": label,
                "settings_sh": settings_sh,
                "max_concurrent_jobs": max_concurrent_jobs,
                "nice": nice,
                "timeout_s": timeout_s,
                "stall_timeout_s": stall_timeout_s,
                "created_at": created_at,
                "created_epoch": created_epoch,
                "workspace": workspace,
                "workspace_commit": workspace_commit,
                "project_relative": f"{build}/flows/{job_id}",
                "build": build,
                "report_dir": str(report_dir),
                "flow": True,
            }
            _initialize_job(
                files,
                spec=spec,
                state={
                    "job_id": job_id,
                    "kind": str(JobKind.VIVADO_FLOW),
                    "state": str(JobState.QUEUED),
                    "target": target,
                    "project": f"{build}/flows/{job_id}",
                    "run": "flow",
                    "created_at": created_at,
                    "workspace": workspace,
                    "workspace_commit": workspace_commit,
                    "percent": 0,
                    "errors": 0,
                    "critical_warnings": 0,
                    "warnings": 0,
                    "tail": [],
                },
                exe=exe,
                environment=environment,
            )
            files.append_event("queued", f"flow {target} queued", target=target)
            self._wake.set()
        return self.status(job_id)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        jobs = []
        for directory in paths.JOBS.glob("j_*"):
            with contextlib.suppress(Exception):
                jobs.append(self.status(directory.name))
        jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return jobs[: max(1, min(limit, 1000))]

    def status(self, job_id: str) -> dict[str, Any]:
        files = self._files(job_id)
        state = files.read_state()
        current = state.get("state")
        if (
            current
            in {
                str(JobState.STARTING),
                str(JobState.RUNNING),
            }
            and state.get("pid") is not None
            and not process_matches(state.get("pid"), state.get("start_ticks"))
        ):
            state = self._mark_lost(files, state)
        state["last_seq"] = files.last_seq()
        state["elapsed_s"] = self._elapsed(state)
        return state

    def events(
        self, job_id: str, since_seq: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        files = self._files(job_id)
        events = files.read_events(max(0, since_seq), max(1, min(limit, 1000)))
        last_seq = events[-1]["seq"] if events else files.last_seq()
        return {
            "job_id": job_id,
            "events": events,
            "last_seq": last_seq,
            "state": self.status(job_id)["state"],
        }

    async def wait(
        self, job_id: str, timeout_s: float, since_seq: int | None
    ) -> dict[str, Any]:
        timeout_s = max(0.0, min(timeout_s, 3600.0))
        files = self._files(job_id)
        cursor = files.last_seq() if since_seq is None else max(0, since_seq)
        deadline = time.monotonic() + timeout_s
        while True:
            status = self.status(job_id)
            terminal = status["state"] in TERMINAL_JOB_STATES
            fresh = files.read_events(cursor, 200)
            important = [event for event in fresh if event["type"] in _IMPORTANT_EVENTS]
            if terminal or important:
                return {
                    "job": status,
                    "events": fresh,
                    "last_seq": fresh[-1]["seq"] if fresh else status["last_seq"],
                    "timed_out": False,
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "job": status,
                    "events": fresh,
                    "last_seq": fresh[-1]["seq"] if fresh else status["last_seq"],
                    "timed_out": True,
                }
            await asyncio.sleep(min(0.5, remaining))

    def cancel(self, job_id: str) -> dict[str, Any]:
        files = self._files(job_id)
        state = files.read_state()
        if state.get("state") in TERMINAL_JOB_STATES:
            return state
        state = files.update_state(
            unless_terminal=True,
            state=str(JobState.CANCELLED),
            finished_at=utc_now(),
        )
        if state.get("state") != str(JobState.CANCELLED):
            return state
        files.append_event("finished", "cancelled", state=str(JobState.CANCELLED))
        pid, pgid = state.get("pid"), state.get("pgid")
        if pgid and process_matches(pid, state.get("start_ticks")):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(pgid), signal.SIGTERM)
        self._wake.set()
        return self.status(job_id)

    def logs(
        self, job_id: str, tail: int = 100, grep: str | None = None
    ) -> dict[str, Any]:
        files = self._files(job_id)
        if not files.stdout.exists():
            return {"job_id": job_id, "lines": [], "count": 0}
        pattern: re.Pattern[str] | None = None
        if grep:
            try:
                pattern = re.compile(grep)
            except re.error as exc:
                raise BadRequest(f"invalid grep regex: {exc}") from exc
        kept: deque[str] = deque(maxlen=max(1, min(tail, 2000)))
        with files.stdout.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                text = line.rstrip("\n")
                if pattern is None or pattern.search(text):
                    kept.append(text[-4000:])
        return {"job_id": job_id, "lines": list(kept), "count": len(kept)}

    def messages(self, job_id: str, min_severity: str = "warning") -> dict[str, Any]:
        files = self._files(job_id)
        if min_severity not in {"warning", "critical_warning", "error", "fatal"}:
            raise BadRequest(
                "min_severity must be warning, critical_warning, error, or fatal"
            )
        lines: deque[str] = deque(maxlen=200_000)
        spec = read_json(files.spec)
        project = Path(spec["project"])
        run_log = (
            project.parent
            / f"{project.stem}.runs"
            / spec.get("run", "synth_1")
            / "runme.log"
        )
        stdout_lines: list[str] = []
        if files.stdout.exists():
            with files.stdout.open("r", encoding="utf-8", errors="replace") as fh:
                stdout_lines = list(fh)
                lines.extend(stdout_lines)
        if run_log.exists():
            unmatched = Counter(stdout_lines)
            with run_log.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if unmatched[line] > 0:
                        unmatched[line] -= 1
                    else:
                        lines.append(line)
        messages = parse_messages(list(lines), min_severity)
        return {
            "job_id": job_id,
            "messages": messages[:500],
            "count": len(messages),
            "truncated": len(messages) > 500,
        }

    def artifacts(self, job_id: str) -> dict[str, Any]:
        files = self._files(job_id)
        result = read_json(files.artifacts)
        spec = read_json(files.spec)
        result["workspace"] = spec.get("workspace")
        result["build"] = spec.get("build", "build")
        return result

    def report_inputs(self, job_id: str) -> dict[str, Any]:
        """Return agent-internal paths needed by the report service."""
        files = self._files(job_id)
        return {
            "spec": read_json(files.spec),
            "artifacts": read_json(files.artifacts).get("artifacts", []),
        }

    async def scheduler_loop(self) -> None:
        while True:
            try:
                await self._schedule()
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except TimeoutError:
                    pass
            except Exception:  # pylint: disable=broad-exception-caught
                log.exception("job scheduler iteration failed")
                await asyncio.sleep(1.0)

    async def _schedule(self) -> None:
        async with self._lock:
            items: list[tuple[str, JobFiles, dict[str, Any]]] = []
            running = 0
            for directory in paths.JOBS.glob("j_*"):
                files = JobFiles(directory)
                with contextlib.suppress(Exception):
                    state = files.read_state()
                    if state.get("state") in {
                        str(JobState.STARTING),
                        str(JobState.RUNNING),
                    }:
                        if state.get("pid") is None or process_matches(
                            state.get("pid"), state.get("start_ticks")
                        ):
                            running += 1
                        else:
                            self._mark_lost(files, state)
                    elif state.get("state") == str(JobState.QUEUED):
                        items.append((state.get("created_at", ""), files, state))
            items.sort(key=lambda item: item[0])
            for _created, files, _state in items:
                spec = read_json(files.spec)
                max_jobs = int(spec.get("max_concurrent_jobs", 1))
                if running >= max_jobs:
                    break
                await self._launch(files)
                running += 1

    async def _launch(self, files: JobFiles) -> None:
        files.update_state(state=str(JobState.STARTING))
        runner_log = (files.directory / "runner.log").open("ab", buffering=0)
        try:
            proc = await asyncio.create_subprocess_exec(
                *worker_argv(files.directory),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=runner_log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                env=os.environ.copy(),
            )
        finally:
            runner_log.close()
        current = files.read_state()
        if current.get("state") == str(JobState.STARTING) and not current.get("pid"):
            files.update_state(pid=proc.pid)
        elif current.get("state") in TERMINAL_JOB_STATES:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)

    def _find_idempotent(self, key: str) -> str | None:
        for directory in paths.JOBS.glob("j_*"):
            with contextlib.suppress(Exception):
                if read_json(directory / "spec.json").get("idempotency_key") == key:
                    return directory.name
        return None

    def _idempotent_replay(self, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        existing = self._find_idempotent(key)
        if existing is None:
            return None
        result = self.status(existing)
        result["idempotent_replay"] = True
        return result

    @staticmethod
    def _elapsed(state: dict[str, Any]) -> float:
        raw_start = state.get("started_at") or state.get("created_at")
        raw_end = state.get("finished_at")
        if not raw_start:
            return 0.0
        try:
            start = datetime.fromisoformat(raw_start)
            end = datetime.fromisoformat(raw_end) if raw_end else datetime.now(UTC)
            return round(max(0.0, (end - start).total_seconds()), 1)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _mark_lost(files: JobFiles, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("state") in TERMINAL_JOB_STATES:
            return state
        state = files.update_state(
            unless_terminal=True, state=str(JobState.LOST), finished_at=utc_now()
        )
        if state.get("state") != str(JobState.LOST):
            return state
        files.append_event(
            "finished",
            "worker process disappeared without a terminal record",
            state=str(JobState.LOST),
        )
        return state
