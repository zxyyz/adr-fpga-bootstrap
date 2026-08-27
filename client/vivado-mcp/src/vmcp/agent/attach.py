"""stdio <-> unix-socket bridge — the process ssh actually runs.

This is the only piece that dies when the network drops.  It copies bytes
verbatim and holds no state, so a reconnect is just a new ssh exec; the daemon,
its sessions and its jobs never notice.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys

from . import paths
from .daemon import payload_path

_CHUNK = 1 << 16
_CONNECT_ATTEMPTS = 40
_CONNECT_DELAY = 0.25


def daemon_argv() -> list[str]:
    payload = payload_path()
    if payload is not None:
        return [sys.executable, payload, "serve"]
    return [sys.executable, "-m", "vmcp.agent", "serve"]


def spawn_daemon() -> None:
    paths.ensure_dirs()
    argv = daemon_argv()
    with open(paths.DAEMON_LOG, "ab") as logfh:
        # Deliberately not a context manager: the daemon must outlive this
        # process. start_new_session detaches it from our session so the ssh
        # connection closing cannot signal it.
        subprocess.Popen(  # pylint: disable=consider-using-with
            argv,
            stdin=subprocess.DEVNULL,
            stdout=logfh,
            stderr=subprocess.STDOUT,
            cwd=str(paths.HOME),
            env=dict(os.environ),
            start_new_session=True,
        )
    print(f"vmcp: started daemon: {' '.join(argv)}", file=sys.stderr)


async def _connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    spawned = False
    last: OSError | None = None
    for attempt in range(_CONNECT_ATTEMPTS):
        try:
            return await asyncio.open_unix_connection(str(paths.DAEMON_SOCKET))
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            last = exc
            if not spawned:
                spawn_daemon()
                spawned = True
            await asyncio.sleep(_CONNECT_DELAY * min(attempt + 1, 4))
    raise SystemExit(
        f"vmcp: daemon did not come up at {paths.DAEMON_SOCKET}: {last}\n"
        f"vmcp: see {paths.DAEMON_LOG}"
    )


async def _stdio() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=_CHUNK * 16)
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    return reader, writer


async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while chunk := await src.read(_CHUNK):
            dst.write(chunk)
            await dst.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        with contextlib.suppress(OSError):
            dst.close()


async def attach() -> int:
    sock_reader, sock_writer = await _connect()
    in_reader, out_writer = await _stdio()
    up = asyncio.create_task(_pump(in_reader, sock_writer), name="vmcp-up")
    down = asyncio.create_task(_pump(sock_reader, out_writer), name="vmcp-down")
    done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for task in done:
        task.result()
    return 0
