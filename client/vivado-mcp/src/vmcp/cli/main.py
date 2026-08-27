"""``vmcp`` — the same link, driven from a shell.

This provides direct transport diagnostics and a shell-friendly
``vmcp wait <job_id>`` command for waiting on detached builds without polling
through an MCP client.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from ..common.errors import VmcpError
from ..common.models import JobState, TERMINAL_JOB_STATES
from ..server.context import Ctx


async def _run(args: argparse.Namespace) -> int:
    ctx = Ctx.create()
    try:
        result: Any
        if args.cmd == "status":
            await ctx.link.ensure_payload(force=args.force_deploy)
            result = await ctx.link.call(
                "host_status",
                {
                    "tools": [t.to_spec() for t in ctx.cfg.host.tools],
                    "max_concurrent_jobs": ctx.cfg.host.max_concurrent_jobs,
                },
                timeout=180.0,
            )
            result["link"] = ctx.link.describe()
        elif args.cmd == "ensure":
            result = await ctx.link.ensure_payload(force=args.force)
        elif args.cmd == "sessions":
            result = await ctx.link.call("session_list", timeout=60.0)
        elif args.cmd == "open":
            await ctx.link.ensure_payload()
            cfg = ctx.cfg.host.tool(args.tool)
            result = await ctx.link.call(
                "session_open",
                {
                    "kind": cfg.kind,
                    "label": cfg.label,
                    "settings_sh": cfg.settings_sh,
                    "cwd": args.cwd,
                },
                timeout=300.0,
            )
        elif args.cmd == "close":
            result = await ctx.link.call(
                "session_close", {"session_id": args.session_id}, timeout=60.0
            )
        elif args.cmd == "eval":
            script = args.script if args.script != "-" else sys.stdin.read()
            result = await ctx.link.call(
                "session_eval",
                {
                    "session_id": args.session_id,
                    "script": script,
                    "timeout_s": args.timeout,
                },
                timeout=args.timeout + 30.0,
            )
        elif args.cmd == "build":
            await ctx.link.ensure_payload()
            cfg = ctx.cfg.host.tool(args.tool)
            result = await ctx.link.call(
                "job_submit",
                {
                    "target": args.target,
                    "project": args.project,
                    "run": args.run,
                    "jobs": args.jobs or ctx.cfg.host.default_jobs,
                    "reset": args.reset,
                    "strategy": args.strategy,
                    "idempotency_key": args.idempotency_key,
                    "label": cfg.label,
                    "settings_sh": cfg.settings_sh,
                    "max_concurrent_jobs": ctx.cfg.host.max_concurrent_jobs,
                    "nice": ctx.cfg.host.nice,
                    "timeout_s": args.timeout,
                    "stall_timeout_s": ctx.cfg.host.stall_timeout_s,
                },
                timeout=300.0,
            )
        elif args.cmd == "jobs":
            result = await ctx.link.call(
                "job_list", {"limit": args.limit}, timeout=60.0
            )
        elif args.cmd == "job-status":
            result = await ctx.link.call(
                "job_status", {"job_id": args.job_id}, timeout=60.0
            )
        elif args.cmd == "logs":
            result = await ctx.link.call(
                "job_logs",
                {"job_id": args.job_id, "tail": args.tail, "grep": args.grep},
                timeout=60.0,
            )
        elif args.cmd == "cancel":
            result = await ctx.link.call(
                "job_cancel", {"job_id": args.job_id}, timeout=60.0
            )
        elif args.cmd == "wait":
            return await _wait(ctx, args.job_id, args.interval)
        else:  # pragma: no cover - argparse enforces the set
            raise AssertionError(args.cmd)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    finally:
        await ctx.link.aclose()


async def _wait(ctx: Ctx, job_id: str, interval: float) -> int:
    cursor = 0
    while True:
        result = await ctx.link.call(
            "job_wait",
            {"job_id": job_id, "timeout_s": interval, "since_seq": cursor},
            timeout=interval + 30.0,
        )
        cursor = int(result["last_seq"])
        job = result["job"]
        if job["state"] not in TERMINAL_JOB_STATES:
            continue
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if job["state"] == str(JobState.SUCCEEDED):
            return 0
        if job["state"] == str(JobState.CANCELLED):
            return 130
        if job["state"] == str(JobState.TIMEOUT):
            return 124
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vmcp", description=__doc__)
    parser.add_argument("--log-level", default="WARNING")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="host, agent and tool inventory")
    p_status.add_argument("--force-deploy", action="store_true")

    p_ensure = sub.add_parser("ensure", help="deploy or upgrade the remote agent")
    p_ensure.add_argument("--force", action="store_true")

    sub.add_parser("sessions", help="list live tool sessions")

    p_open = sub.add_parser("open", help="open a tool session")
    p_open.add_argument("--tool")
    p_open.add_argument("--cwd")

    p_close = sub.add_parser("close", help="close a tool session")
    p_close.add_argument("session_id")

    p_eval = sub.add_parser("eval", help="run a Tcl script in a session")
    p_eval.add_argument("session_id")
    p_eval.add_argument("script", help="Tcl script, or - to read stdin")
    p_eval.add_argument("--timeout", type=float, default=120.0)

    p_build = sub.add_parser("build", help="start a detached Vivado build")
    p_build.add_argument("project", help="existing remote .xpr path")
    p_build.add_argument(
        "--target", choices=("synth", "impl", "bitstream", "flow"), default="bitstream"
    )
    p_build.add_argument("--run")
    p_build.add_argument("--jobs", type=int)
    p_build.add_argument("--reset", action="store_true")
    p_build.add_argument("--strategy")
    p_build.add_argument("--idempotency-key")
    p_build.add_argument("--tool")
    p_build.add_argument("--timeout", type=float, default=0.0)

    p_jobs = sub.add_parser("jobs", help="list durable jobs")
    p_jobs.add_argument("--limit", type=int, default=100)

    p_job_status = sub.add_parser("job-status", help="show one job")
    p_job_status.add_argument("job_id")

    p_logs = sub.add_parser("logs", help="show a job log tail")
    p_logs.add_argument("job_id")
    p_logs.add_argument("--tail", type=int, default=100)
    p_logs.add_argument("--grep")

    p_cancel = sub.add_parser("cancel", help="cancel a job")
    p_cancel.add_argument("job_id")

    p_wait = sub.add_parser("wait", help="wait until a job reaches a terminal state")
    p_wait.add_argument("job_id")
    p_wait.add_argument("--interval", type=float, default=600.0)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return asyncio.run(_run(args))
    except VmcpError as exc:
        print(f"vmcp: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
