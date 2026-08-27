"""Detached build jobs and their compact observation tools."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ...common.errors import BadRequest
from ..context import Ctx, clamp
from ..workspace import validate_relative


def register(server: MCPServer, ctx: Ctx) -> None:
    @server.tool(
        description=(
            "Start a detached Vivado project build and return immediately with a "
            "job_id. The build survives SSH and daemon restarts. With a configured "
            "workspace, project is workspace-relative and the exact synced commit "
            "is recorded in env.json. "
            "target is synth, impl, bitstream, or flow. Supply an idempotency_key "
            "when retrying a mutation so a lost reply cannot launch a duplicate."
        )
    )
    async def build(
        project: str,
        target: str = "bitstream",
        run: str | None = None,
        jobs: int | None = None,
        reset: bool = False,
        strategy: str | None = None,
        idempotency_key: str | None = None,
        tool: str | None = None,
        timeout_s: float = 0.0,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        await ctx.link.ensure_payload()
        cfg = ctx.cfg.host.tool(tool)
        if cfg.kind != "vivado":
            raise BadRequest("project build jobs require a Vivado tool")
        workspace_cfg = None
        project_value = project
        if ctx.cfg.workspaces or workspace is not None:
            workspace_cfg = ctx.cfg.workspace(workspace)
            project_value = validate_relative(project)
        return await ctx.link.call(
            "job_submit",
            {
                "target": target,
                "project": project_value,
                "workspace": workspace_cfg.name if workspace_cfg else None,
                "build": workspace_cfg.build if workspace_cfg else "build",
                "run": run,
                "jobs": jobs or ctx.cfg.host.default_jobs,
                "reset": reset,
                "strategy": strategy,
                "idempotency_key": idempotency_key,
                "label": cfg.label,
                "settings_sh": cfg.settings_sh,
                "max_concurrent_jobs": ctx.cfg.host.max_concurrent_jobs,
                "nice": ctx.cfg.host.nice,
                "timeout_s": timeout_s,
                "stall_timeout_s": ctx.cfg.host.stall_timeout_s,
            },
            timeout=300.0,
        )

    @server.tool(description="List durable build jobs, newest first.")
    async def job_list(limit: int = 100) -> dict[str, Any]:
        jobs_result = await ctx.link.call("job_list", {"limit": limit}, timeout=60.0)
        return {"jobs": jobs_result, "count": len(jobs_result)}

    @server.tool(
        description=(
            "Return one compact job snapshot: state, current Vivado step/phase, "
            "monotonic percent estimate, elapsed time, message counts and log tail."
        )
    )
    async def job_status(job_id: str) -> dict[str, Any]:
        return await ctx.link.call("job_status", {"job_id": job_id}, timeout=60.0)

    @server.tool(
        description=(
            "Read append-only job events after since_seq. Use the returned last_seq "
            "as the next cursor so no event is repeated or lost."
        )
    )
    async def job_events(
        job_id: str, since_seq: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        return await ctx.link.call(
            "job_events",
            {"job_id": job_id, "since_seq": since_seq, "limit": limit},
            timeout=60.0,
        )

    @server.tool(
        description=(
            "Long-poll a job without repeated client polling. Returns on terminal "
            "state, an important event (error/stall/artifact), or timeout. Pass the "
            "returned last_seq on the next call. Set MCP_TOOL_TIMEOUT above timeout_s."
        )
    )
    async def job_wait(
        request_ctx: Context,
        job_id: str,
        timeout_s: float = 600.0,
        since_seq: int | None = None,
    ) -> dict[str, Any]:
        timeout_s = max(0.0, min(timeout_s, 3600.0))
        pending = asyncio.create_task(
            ctx.link.call(
                "job_wait",
                {"job_id": job_id, "timeout_s": timeout_s, "since_seq": since_seq},
                timeout=timeout_s + 30.0,
            )
        )
        started = time.monotonic()
        try:
            while True:
                done, _pending = await asyncio.wait({pending}, timeout=15.0)
                if done:
                    return await pending
                elapsed = time.monotonic() - started
                await request_ctx.report_progress(
                    min(elapsed, timeout_s),
                    timeout_s,
                    f"waiting for {job_id}",
                )
        finally:
            if not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending

    @server.tool(description="Cancel a queued or running job and its process group.")
    async def job_cancel(job_id: str) -> dict[str, Any]:
        return await ctx.link.call("job_cancel", {"job_id": job_id}, timeout=60.0)

    @server.tool(
        description=(
            "Return only the tail of a job log, optionally filtered by a regex. "
            "Never returns the complete multi-megabyte Vivado log."
        )
    )
    async def job_logs(
        job_id: str, tail: int = 100, grep: str | None = None
    ) -> dict[str, Any]:
        result = await ctx.link.call(
            "job_logs", {"job_id": job_id, "tail": tail, "grep": grep}, timeout=60.0
        )
        text, truncated = clamp("\n".join(result.pop("lines", [])))
        result["log"] = text
        if truncated:
            result["truncated"] = True
        return result

    @server.tool(
        description=(
            "Deduplicate Vivado warnings/errors by message ID, returning severity, "
            "count and one example instead of flooding the context with repeated lines."
        )
    )
    async def messages(job_id: str, min_severity: str = "warning") -> dict[str, Any]:
        return await ctx.link.call(
            "job_messages",
            {"job_id": job_id, "min_severity": min_severity},
            timeout=60.0,
        )

    @server.tool(description="List DCP, bitstream, report and related job artifacts.")
    async def job_artifacts(job_id: str) -> dict[str, Any]:
        return await ctx.link.call("job_artifacts", {"job_id": job_id}, timeout=60.0)
