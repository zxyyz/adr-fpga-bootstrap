"""A long-lived Tcl-shell tool session (Vivado ``-mode tcl``, ``vitis_hls -i``).

A session normally runs only short, interactive commands. Synthesis and
implementation use detached jobs, so this class treats a timeout as a fault
rather than normal operation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from ...common.errors import EvalTimeout, SessionBusy, SessionDead, SessionError
from ...common.ids import new_id
from ...common.models import EvalResult, SessionInfo, SessionState, ToolKind
from ...common.tcl import b64encode_utf8
from .bootstrap_tcl import BEGIN, BOOTSTRAP_TCL, END, READY, SENTINEL_SUFFIX

log = logging.getLogger(__name__)

#: Vivado prints ``Vivado% `` (and vitis_hls ``vitis_hls> ``) with no trailing
#: newline, so the prompt arrives glued to the front of the next output line.
_PROMPT_RE = re.compile(r"^(?:Vivado%\s?|vitis_hls>\s?)+")

#: The result of a command may legitimately be a whole report; keep the stream
#: limit well above any single line we expect.
_STREAM_LIMIT = 64 * 1024 * 1024

_LOG_KEEP_LINES = 4000


class _Pending:
    __slots__ = ("uuid", "future", "log", "dropped", "started")

    def __init__(self, uuid: str) -> None:
        self.uuid = uuid
        self.future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self.log: deque[str] = deque(maxlen=_LOG_KEEP_LINES)
        self.dropped = 0
        self.started = time.monotonic()


class TclShellSession:
    def __init__(
        self,
        *,
        session_id: str,
        kind: str,
        label: str,
        exe: str,
        args: list[str],
        source_flag: str,
        env: dict[str, str],
        cwd: Path,
        directory: Path,
        version: str | None = None,
        boot_timeout: float = 180.0,
    ) -> None:
        self.session_id = session_id
        self.kind = kind
        self.label = label
        self.version = version
        self.cwd = cwd
        self.dir = directory
        self.state = SessionState.BOOTING

        self._exe = exe
        self._args = args
        self._source_flag = source_flag
        self._env = env
        self._boot_timeout = boot_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._log_fh: IO[str] | None = None
        self._ready = asyncio.Event()
        self._cmd_lock = asyncio.Lock()
        self._pending: dict[str, _Pending] = {}
        self._orphans: set[str] = set()
        self._await_payload: str | None = None
        self._payloads: dict[str, str] = {}
        self._boot_s: float | None = None
        self._last_used = time.monotonic()
        self._commands = 0

    # -- lifecycle ---------------------------------------------------------

    async def boot(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        bootstrap = self.dir / "bootstrap.tcl"
        bootstrap.write_text(BOOTSTRAP_TCL, encoding="utf-8")
        # Line-buffered and held open for the session's lifetime; closed by close().
        self._log_fh = open(  # pylint: disable=consider-using-with
            self.dir / "session.log", "a", buffering=1, encoding="utf-8"
        )

        argv = [self._exe, *self._args, self._source_flag, str(bootstrap)]
        self._log(f"--- vmcp boot {self.session_id} cwd={self.cwd} ---")
        self._log(f"--- argv: {' '.join(argv)}")
        t0 = time.monotonic()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.cwd),
                env=self._env,
                start_new_session=True,
                limit=_STREAM_LIMIT,
            )
        except OSError as exc:
            self.state = SessionState.DEAD
            raise SessionError(f"cannot launch {self._exe}: {exc}") from exc

        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"vmcp-read-{self.session_id}"
        )
        try:
            await asyncio.wait_for(self._ready.wait(), self._boot_timeout)
        except TimeoutError:
            tail = self._tail_log(30)
            await self.close()
            raise SessionError(
                f"{self.label} did not reach READY within {self._boot_timeout:.0f}s. "
                f"Last output:\n{tail}"
            ) from None

        self._boot_s = time.monotonic() - t0
        self.state = SessionState.IDLE
        self._last_used = time.monotonic()
        log.info(
            "session %s (%s) ready in %.2fs", self.session_id, self.label, self._boot_s
        )

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        self.state = SessionState.DEAD
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(OSError, BrokenPipeError):
                assert proc.stdin is not None
                proc.stdin.write(b"exit\n")
                await proc.stdin.drain()
            try:
                await asyncio.wait_for(proc.wait(), 5.0)
            except TimeoutError:
                self._kill(proc, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), 3.0)
                except TimeoutError:
                    self._kill(proc, signal.SIGKILL)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), 3.0)
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        self._fail_pending(SessionDead("session closed"))
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), sig)

    # -- commands ----------------------------------------------------------

    async def eval(self, script: str, timeout: float = 120.0) -> EvalResult:
        if self.state is SessionState.DEAD:
            raise SessionDead(f"session {self.session_id} is dead")
        if self.state is SessionState.BUSY:
            raise SessionBusy(
                f"session {self.session_id} is still executing a previous command"
            )
        if self.state is not SessionState.IDLE:
            raise SessionError(f"session {self.session_id} is {self.state}")

        async with self._cmd_lock:
            assert self._proc is not None and self._proc.stdin is not None
            uuid = new_id("c").replace("_", "")
            pending = _Pending(uuid)
            self._pending[uuid] = pending
            self.state = SessionState.BUSY
            self._commands += 1
            payload = b64encode_utf8(script)
            try:
                self._proc.stdin.write(f"vmcp_eval {uuid} {payload}\n".encode("ascii"))
                await self._proc.stdin.drain()
            except (OSError, BrokenPipeError) as exc:
                self._pending.pop(uuid, None)
                self.state = SessionState.DEAD
                raise SessionDead(
                    f"session {self.session_id} stdin is gone: {exc}"
                ) from exc

            try:
                raw = await asyncio.wait_for(pending.future, timeout)
            except TimeoutError:
                # We cannot interrupt a Tcl interpreter mid-command. Keep the
                # session BUSY (and unusable) until its sentinel arrives, so we
                # never interleave two commands on one interpreter.
                self._orphans.add(uuid)
                raise EvalTimeout(
                    f"no result from session {self.session_id} within {timeout:.0f}s; "
                    f"the command is still running and the session is unusable until "
                    f"it finishes (log: {self.dir / 'session.log'})"
                ) from None
            finally:
                self._last_used = time.monotonic()

        return EvalResult(
            rc=int(raw.get("rc", 1)),
            result=str(raw.get("result", "")),
            errorinfo=str(raw.get("errorinfo", "")),
            log="\n".join(pending.log),
            log_lines_dropped=pending.dropped,
            elapsed_s=round(time.monotonic() - pending.started, 3),
        )

    async def health(self) -> bool:
        if self.state is not SessionState.IDLE:
            return False
        try:
            return (await self.eval("expr 1", timeout=15.0)).ok
        except SessionError:
            return False

    def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            kind=self.kind,
            label=self.label,
            state=str(self.state),
            cwd=str(self.cwd),
            pid=self._proc.pid if self._proc else None,
            version=self.version,
            boot_s=round(self._boot_s, 2) if self._boot_s else None,
            idle_s=round(time.monotonic() - self._last_used, 1),
            commands=self._commands,
            log_path=str(self.dir / "session.log"),
        )

    @property
    def idle_s(self) -> float:
        return time.monotonic() - self._last_used

    # -- reader ------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                try:
                    raw = await stdout.readline()
                except ValueError:
                    # Line longer than _STREAM_LIMIT: the stream position is
                    # unrecoverable, so the session is finished.
                    raise SessionDead(
                        f"session {self.session_id} emitted a line over "
                        f"{_STREAM_LIMIT} bytes"
                    ) from None
                if not raw:
                    break
                self._on_line(
                    _PROMPT_RE.sub("", raw.decode("utf-8", "replace").rstrip("\r\n"))
                )
        # Whatever goes wrong here, callers must be told rather than left waiting
        # on a future the reader will never resolve. CancelledError derives from
        # BaseException, so close() still cancels this task normally.
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.exception("session %s reader failed", self.session_id)
            self._fail_pending(SessionDead(str(exc)))
        finally:
            if self.state is not SessionState.DEAD:
                self.state = SessionState.DEAD
                rc = self._proc.returncode if self._proc else None
                self._fail_pending(
                    SessionDead(f"session {self.session_id} exited (rc={rc})")
                )

    def _on_line(self, line: str) -> None:
        # A BEGIN sentinel is always followed by exactly one JSON line.
        if self._await_payload is not None:
            self._payloads[self._await_payload] = line
            self._await_payload = None
            return
        if line == READY:
            self._ready.set()
            return
        if line.startswith(BEGIN) and line.endswith(SENTINEL_SUFFIX):
            self._await_payload = line[len(BEGIN) : -len(SENTINEL_SUFFIX)]
            return
        if line.startswith(END) and line.endswith(SENTINEL_SUFFIX):
            self._complete(line[len(END) : -len(SENTINEL_SUFFIX)])
            return
        self._log(line)
        for pending in self._pending.values():
            if len(pending.log) == pending.log.maxlen:
                pending.dropped += 1
            pending.log.append(line)

    def _complete(self, uuid: str) -> None:
        raw = self._payloads.pop(uuid, None)
        pending = self._pending.pop(uuid, None)
        if uuid in self._orphans:
            self._orphans.discard(uuid)
            log.warning(
                "session %s: late result for timed-out command %s",
                self.session_id,
                uuid,
            )
        if pending is not None and not pending.future.done():
            if raw is None:
                pending.future.set_exception(
                    SessionError(f"missing result payload for command {uuid}")
                )
            else:
                try:
                    pending.future.set_result(json.loads(raw))
                except ValueError as exc:
                    pending.future.set_exception(
                        SessionError(f"unparseable result payload for {uuid}: {exc}")
                    )
        if not self._pending and not self._orphans and self.state is SessionState.BUSY:
            self.state = SessionState.IDLE

    def _fail_pending(self, exc: BaseException) -> None:
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(exc)
        self._pending.clear()
        self._orphans.clear()

    def _log(self, line: str) -> None:
        if self._log_fh is not None:
            with contextlib.suppress(OSError):
                self._log_fh.write(line + "\n")

    def _tail_log(self, lines: int) -> str:
        path = self.dir / "session.log"
        if not path.exists():
            return "(no output)"
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """How to start one Tcl-shell tool and hand it the bootstrap script."""

    exe: str
    args: tuple[str, ...]
    source_flag: str


#: Vitis Unified is absent on purpose because it is a gRPC server, not a Tcl shell.
TOOL_SPECS: dict[str, ToolSpec] = {
    ToolKind.VIVADO: ToolSpec(
        exe="vivado",
        args=("-mode", "tcl", "-nolog", "-nojournal", "-notrace"),
        source_flag="-source",
    ),
    ToolKind.VITIS_HLS: ToolSpec(exe="vitis_hls", args=("-i",), source_flag="-f"),
}


def build_session(
    *,
    kind: str,
    label: str,
    exe: str,
    env: dict[str, str],
    cwd: Path,
    directory: Path,
    version: str | None,
    boot_timeout: float,
) -> TclShellSession:
    spec = TOOL_SPECS[kind]
    return TclShellSession(
        session_id=directory.name,
        kind=kind,
        label=label,
        exe=exe,
        args=list(spec.args),
        source_flag=spec.source_flag,
        env=env,
        cwd=cwd,
        directory=directory,
        version=version,
        boot_timeout=boot_timeout,
    )
