"""Detached worker entry point for one durable job."""

from __future__ import annotations

import codecs
import contextlib
import os
import selectors
import signal
import subprocess
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any

from ...common.jsonio import read_json, write_json
from ...common.models import JobState, TERMINAL_JOB_STATES
from .progress import VivadoProgressParser
from .storage import JobFiles, utc_now

_ARTIFACT_SUFFIXES = {".bit", ".bin", ".dcp", ".ltx", ".rpt", ".xsa"}


class _CrossSourceDeduper:
    """Pair identical progress lines seen in stdout and runme.log exactly once."""

    def __init__(self) -> None:
        self._unmatched: dict[str, Counter[str]] = {
            "stdout": Counter(),
            "runlog": Counter(),
        }

    def accept(self, source: str, line: str) -> bool:
        text = line.strip()
        if not (
            text.startswith(
                (
                    "Command:",
                    "Phase ",
                    "WARNING:",
                    "CRITICAL WARNING:",
                    "ERROR:",
                    "FATAL:",
                )
            )
            or " completed successfully" in text
        ):
            return True
        other = "runlog" if source == "stdout" else "stdout"
        if self._unmatched[other][text] > 0:
            self._unmatched[other][text] -= 1
            return False
        self._unmatched[source][text] += 1
        return True


def process_start_ticks(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return int(fields[21])
    except (FileNotFoundError, IndexError, OSError, ValueError):
        return None


def process_matches(pid: int | None, start_ticks: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    current = process_start_ticks(pid)
    return start_ticks is None or current is None or current == start_ticks


def _save_progress(files: JobFiles, state: dict[str, Any]) -> dict[str, Any]:
    fields = {
        name: state.get(name)
        for name in (
            "step",
            "phase",
            "percent",
            "errors",
            "critical_warnings",
            "warnings",
            "tail",
        )
    }
    return files.update_state(unless_terminal=True, **fields)


def _feed_lines(
    files: JobFiles,
    parser: VivadoProgressParser,
    state: dict[str, Any],
    tail: deque[str],
    lines: list[str],
    source: str,
    deduper: _CrossSourceDeduper,
) -> dict[str, Any]:
    for line in lines:
        if not deduper.accept(source, line):
            continue
        if line.strip():
            tail.append(line.rstrip()[-1000:])
        parsed = parser.feed(line)
        for event in parsed.events:
            # Ordinary warnings are counted in state and available via messages(),
            # but do not flood the event cursor.
            if event["type"] != "warning":
                data = {
                    key: value
                    for key, value in event.items()
                    if key not in {"type", "message"}
                }
                files.append_event(event["type"], event.get("message", ""), **data)
        state.update(parsed.changes)
    state["tail"] = list(tail)
    return _save_progress(files, state)


def _read_run_log(path: Path, offset: int, pending: str) -> tuple[int, str, list[str]]:
    try:
        size = path.stat().st_size
    except OSError:
        return offset, pending, []
    if size < offset:
        offset, pending = 0, ""
    if size == offset:
        return offset, pending, []
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    offset += len(chunk)
    text = pending + chunk.decode("utf-8", "replace")
    lines = text.split("\n")
    return offset, lines.pop(), lines


def _finish(
    files: JobFiles, proc: subprocess.Popen[bytes], state: dict[str, Any]
) -> int:
    current = files.read_state()
    if current.get("state") in TERMINAL_JOB_STATES:
        return 0
    rc = int(proc.returncode or 0)
    if rc == 0 and int(state.get("errors", 0)) == 0:
        terminal = JobState.SUCCEEDED
    elif rc in {137, -signal.SIGKILL}:
        terminal = JobState.OOM
    else:
        terminal = JobState.FAILED
    finished = utc_now()
    percent = 100 if terminal is JobState.SUCCEEDED else int(state.get("percent", 0))
    artifacts = _collect_artifacts(files, read_json(files.spec))
    updated = files.update_state(
        unless_terminal=True,
        state=str(terminal),
        finished_at=finished,
        exit_code=rc,
        percent=percent,
    )
    if updated.get("state") != str(terminal):
        return 0
    for artifact in artifacts:
        files.append_event("artifact", artifact["path"], **artifact)
    files.append_event(
        "finished", str(terminal), state=str(terminal), exit_code=rc, percent=percent
    )
    return 0 if terminal is JobState.SUCCEEDED else 1


def _collect_artifacts(files: JobFiles, spec: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(spec["project"]).parent
    started = float(spec.get("created_epoch", 0.0))
    found: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for path in root.rglob("*"):
            if len(found) >= 500:
                break
            try:
                stat = path.stat()
            except OSError:
                continue
            if (
                path.is_file()
                and path.suffix.lower() in _ARTIFACT_SUFFIXES
                and stat.st_mtime >= started
            ):
                found.append(
                    {
                        "path": str(path),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "kind": path.suffix.lower().lstrip("."),
                    }
                )
    found.sort(key=lambda item: item["path"])
    write_json(files.artifacts, {"artifacts": found, "truncated": len(found) >= 500})
    return found


def _terminate_for_timeout(files: JobFiles, timeout_s: float) -> None:
    finished = utc_now()
    updated = files.update_state(
        unless_terminal=True, state=str(JobState.TIMEOUT), finished_at=finished
    )
    if updated.get("state") == str(JobState.TIMEOUT):
        files.append_event(
            "finished",
            "wall-clock timeout",
            state=str(JobState.TIMEOUT),
            timeout_s=timeout_s,
        )
    os.killpg(os.getpgrp(), signal.SIGTERM)


# A worker owns process launch, stream decoding, progress parsing and timeout
# supervision in one synchronous lifecycle; splitting it would obscure ordering.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
def run_job(directory: str) -> int:
    files = JobFiles(Path(directory).resolve())
    spec, env_data = read_json(files.spec), read_json(files.env)
    started_at = utc_now()
    pid, pgid = os.getpid(), os.getpgrp()
    state = files.update_state(
        unless_terminal=True,
        state=str(JobState.STARTING),
        pid=pid,
        pgid=pgid,
        start_ticks=process_start_ticks(pid),
        started_at=started_at,
    )
    if state.get("state") in TERMINAL_JOB_STATES:
        return 0
    files.append_event("started", "detached worker started", pid=pid, pgid=pgid)

    nice = int(spec.get("nice", 10))
    if nice:
        with contextlib.suppress(OSError):
            os.nice(nice)
    argv = [
        env_data["exe"],
        "-mode",
        "batch",
        "-nojournal",
        "-notrace",
        "-log",
        str(files.vivado_log),
        "-source",
        str(files.script),
    ]
    env = {str(k): str(v) for k, v in env_data["environment"].items()}
    project_path = Path(spec["project"])
    run_log = (
        project_path.parent
        / f"{project_path.stem}.runs"
        / spec.get("run", "synth_1")
        / "runme.log"
    )
    if spec.get("reset"):
        run_log.unlink(missing_ok=True)
    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        argv,
        cwd=spec["cwd"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    state = files.update_state(
        unless_terminal=True, state=str(JobState.RUNNING), tool_pid=proc.pid
    )
    if state.get("state") in TERMINAL_JOB_STATES:
        proc.terminate()
        with contextlib.suppress(TimeoutError):
            proc.wait(timeout=5)
        return 0

    assert proc.stdout is not None
    os.set_blocking(proc.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    run_pending = ""
    try:
        run_offset = run_log.stat().st_size
    except OSError:
        run_offset = 0
    tail: deque[str] = deque(maxlen=10)
    parser = VivadoProgressParser(state)
    deduper = _CrossSourceDeduper()
    began = time.monotonic()
    last_output = began
    last_stall_event = 0.0
    timeout_s = float(spec.get("timeout_s", 0.0))
    stall_timeout_s = float(spec.get("stall_timeout_s", 900.0))

    with files.stdout.open("ab", buffering=0) as log:
        while proc.poll() is None:
            for key, _mask in selector.select(timeout=1.0):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    continue
                log.write(chunk)
                last_output = time.monotonic()
                pending += decoder.decode(chunk)
                lines = pending.split("\n")
                pending = lines.pop()
                state = _feed_lines(
                    files, parser, state, tail, lines, "stdout", deduper
                )

            run_offset, run_pending, run_lines = _read_run_log(
                run_log, run_offset, run_pending
            )
            if run_lines:
                last_output = time.monotonic()
                state = _feed_lines(
                    files, parser, state, tail, run_lines, "runlog", deduper
                )

            now = time.monotonic()
            if 0 < timeout_s <= now - began:
                _terminate_for_timeout(files, timeout_s)
            if (
                0 < stall_timeout_s <= now - last_output
                and stall_timeout_s <= now - last_stall_event
            ):
                last_stall_event = now
                files.append_event(
                    "stalled",
                    f"no log output for {int(now - last_output)} seconds",
                    silent_s=int(now - last_output),
                )

        # Drain bytes produced immediately before process exit.
        while chunk := os.read(proc.stdout.fileno(), 65536):
            log.write(chunk)
            pending += decoder.decode(chunk)
        pending += decoder.decode(b"", final=True)
        state = _feed_lines(
            files, parser, state, tail, pending.splitlines(), "stdout", deduper
        )
        _run_offset, run_pending, run_lines = _read_run_log(
            run_log, run_offset, run_pending
        )
        state = _feed_lines(
            files,
            parser,
            state,
            tail,
            run_lines + run_pending.splitlines(),
            "runlog",
            deduper,
        )
    return _finish(files, proc, state)


def worker_argv(directory: Path) -> list[str]:
    payload = os.environ.get("VMCP_PAYLOAD") or sys.argv[0]
    if payload.endswith(".pyz"):
        return [sys.executable, payload, "run-job", str(directory)]
    return [sys.executable, "-m", "vmcp.agent", "run-job", str(directory)]
