"""The ssh link to the build server.

One ``ssh ... vmcp-agent attach`` subprocess carries framed JSON both ways.  The
link is disposable by design: if it dies, the daemon and everything it owns keep
running, and the next call transparently establishes a new one.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
from pathlib import Path
from typing import Any

from ... import PROTOCOL_VERSION
from ...common.errors import TransportError
from ...common.framing import MAX_FRAME
from ...common.rpc import Peer
from ..config import HostCfg
from . import payload

log = logging.getLogger(__name__)

#: Calls with no side effects, safe to replay on a fresh link.
_RETRYABLE = frozenset(
    {
        "ping",
        "host_status",
        "session_list",
        "job_list",
        "job_status",
        "job_events",
        "job_wait",
        "job_logs",
        "job_messages",
        "job_artifacts",
        "report_get",
        "report_diff",
        "job_cancel",
        "workspace_prepare",
        "workspace_status",
        "workspace_activate",
        "workspace_resolve",
        "workspace_mkdir",
        "file_read",
        "file_grep",
        "project_claim",
        "project_release",
        "project_binding",
        "project_locks",
        "project_unlock",
    }
)

_STDERR_KEEP = 40


class SshLink:
    def __init__(self, host: HostCfg) -> None:
        self.host = host
        self._peer: Peer | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._stderr: collections.deque[str] = collections.deque(maxlen=_STDERR_KEEP)
        self._lock = asyncio.Lock()
        self._local_sha: str | None = None
        self._verified: dict[str, Any] | None = None
        self.payload_stale = False

    # -- public ------------------------------------------------------------

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = 120.0,
    ) -> Any:
        peer = await self._ensure()
        try:
            return await peer.call(method, params, timeout)
        except TransportError:
            retryable_mutation = method in {"job_submit", "flow_submit"} and bool(
                (params or {}).get("idempotency_key")
            )
            if method not in _RETRYABLE and not retryable_mutation:
                raise
            log.warning("link dropped during %s; reconnecting", method)
            await self._drop()
            peer = await self._ensure()
            return await peer.call(method, params, timeout)

    async def ensure_payload(self, force: bool = False) -> dict[str, Any]:
        """Deploy the agent if missing, incompatible, or explicitly forced.

        Every tool calls this, so the happy path must not cost a round trip:
        once the running daemon has been verified, remember it. A dropped link
        does not invalidate the answer — the daemon is what was verified, and it
        outlives the link.
        """
        if self._verified is not None and not force:
            return self._verified
        local_pyz, local_sha = payload.build()
        self._local_sha = local_sha
        async with self._lock:
            if force:
                await self._drop()
        self._verified = await self._handshake(local_pyz, local_sha, force)
        return self._verified

    async def _handshake(
        self, local_pyz: Path, local_sha: str, force: bool
    ) -> dict[str, Any]:
        try:
            info = await self.call("ping", timeout=90.0)
        except (TransportError, OSError) as exc:
            log.info("agent not reachable (%s); deploying", exc)
            await self._deploy(local_pyz)
            info = await self.call("ping", timeout=90.0)
            self.payload_stale = False
            return {"action": "deployed", "agent": info}

        protocol_ok = info.get("protocol") == PROTOCOL_VERSION
        if info.get("payload_sha256") == local_sha and protocol_ok and not force:
            self.payload_stale = False
            return {"action": "up_to_date", "agent": info}

        if protocol_ok and not force:
            # Same protocol, different build: leave the running daemon alone so
            # we do not silently kill live sessions. host_status reports this.
            self.payload_stale = True
            return {
                "action": "stale",
                "agent": info,
                "hint": "call agent_ensure(force=true) to upgrade "
                "(this restarts the daemon and drops open sessions)",
            }

        await self._deploy(local_pyz)
        with contextlib.suppress(Exception):
            await self.call("shutdown", timeout=15.0)
        await self._drop()
        info = await self.call("ping", timeout=90.0)
        self.payload_stale = info.get("payload_sha256") != local_sha
        return {"action": "upgraded", "agent": info}

    async def aclose(self) -> None:
        async with self._lock:
            await self._drop()

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    # -- internals ---------------------------------------------------------

    def _ssh_argv(self, remote_command: str) -> list[str]:
        argv = ["ssh"]
        for option in self.host.ssh_options:
            argv += ["-o", option]
        argv += [self.host.ssh, "--", remote_command]
        return argv

    async def _ensure(self) -> Peer:
        async with self._lock:
            if self._peer is not None and not self._peer.closed:
                return self._peer
            await self._drop()
            argv = self._ssh_argv(f"exec {self.host.remote_payload} attach")
            log.info("connecting: %s", " ".join(argv))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=MAX_FRAME,
                )
            except OSError as exc:
                raise TransportError(f"cannot exec ssh: {exc}") from exc

            assert proc.stdout is not None and proc.stdin is not None
            self._proc = proc
            peer = Peer(proc.stdout, proc.stdin, name=f"ssh:{self.host.name}")
            self._peer = peer
            self._tasks = [
                asyncio.create_task(peer.serve(), name="vmcp-ssh-serve"),
                asyncio.create_task(self._drain_stderr(proc), name="vmcp-ssh-stderr"),
            ]
            return peer

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """ssh diagnostics land here; an unread stderr pipe would eventually block."""
        assert proc.stderr is not None
        with contextlib.suppress(Exception):
            while line := await proc.stderr.readline():
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
                    log.info("ssh[%s]: %s", self.host.name, text)

    async def _drop(self) -> None:
        peer, self._peer = self._peer, None
        proc, self._proc = self._proc, None
        tasks, self._tasks = self._tasks, []
        if peer is not None:
            with contextlib.suppress(Exception):
                await peer.aclose()
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), 5.0)
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _deploy(self, local_pyz: Path) -> None:
        remote = self.host.remote_payload
        script = (
            f"set -e; mkdir -p $(dirname {remote}); cat > {remote}.tmp; "
            f"chmod 755 {remote}.tmp; mv {remote}.tmp {remote}"
        )
        argv = self._ssh_argv(script)
        log.info("deploying %s -> %s:%s", local_pyz.name, self.host.ssh, remote)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(local_pyz.read_bytes())
        if proc.returncode != 0:
            raise TransportError(
                f"deploying the agent to {self.host.ssh} failed (rc={proc.returncode}): "
                f"{err.decode(errors='replace').strip()[:500]}"
            )
        if out.strip():
            log.info("deploy output: %s", out.decode(errors="replace").strip())

    def describe(self) -> dict[str, Any]:
        return {
            "host": self.host.name,
            "ssh": self.host.ssh,
            "agent_dir": self.host.agent_dir,
            "connected": self._peer is not None and not self._peer.closed,
            "payload_sha256": self._local_sha,
            "payload_stale": self.payload_stale,
        }
