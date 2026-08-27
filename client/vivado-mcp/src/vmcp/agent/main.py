"""Agent entry point: ``vmcp-agent {serve,attach,info}``.

``attach`` is what ssh execs; it starts ``serve`` on demand.  ``info`` is a cheap
pre-flight the client can run without waking the daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .. import PROTOCOL_VERSION, __version__
from . import paths
from .attach import attach
from .daemon import acquire_singleton, payload_sha256, serve
from .jobs.runner import run_job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vmcp-agent")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the daemon in the foreground")
    sub.add_parser("attach", help="bridge stdio to the daemon, starting it if needed")
    sub.add_parser("info", help="print agent identity as JSON and exit")
    p_run_job = sub.add_parser("run-job", help=argparse.SUPPRESS)
    p_run_job.add_argument("directory")
    args = parser.parse_args(argv)

    if args.cmd == "info":
        print(
            json.dumps(
                {
                    "protocol": PROTOCOL_VERSION,
                    "version": __version__,
                    "payload_sha256": payload_sha256(),
                    "python": sys.version.split()[0],
                    "home": str(paths.HOME),
                    "socket": str(paths.DAEMON_SOCKET),
                    "socket_exists": paths.DAEMON_SOCKET.exists(),
                }
            )
        )
        return 0

    if args.cmd == "attach":
        # stdout is the framed protocol channel, so diagnostics go to stderr only.
        logging.basicConfig(
            level=logging.WARNING,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            stream=sys.stderr,
        )
        return asyncio.run(attach())

    if args.cmd == "run-job":
        return run_job(args.directory)

    assert args.cmd == "serve", args.cmd
    paths.ensure_dirs()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        filename=str(paths.DAEMON_LOG),
    )
    lock = acquire_singleton()
    if lock is None:
        logging.info("another vmcp-agentd already holds %s; exiting", paths.DAEMON_LOCK)
        return 0
    try:
        return asyncio.run(serve())
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
