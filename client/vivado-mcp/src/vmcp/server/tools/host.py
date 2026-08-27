"""Host and agent lifecycle tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..context import Ctx


def register(server: MCPServer, ctx: Ctx) -> None:
    @server.tool(
        description=(
            "Report the build server's health: agent version, discovered Vivado/Vitis "
            "installations and their versions, cores, load, free memory and disk, and "
            "any open tool sessions. Cheap; call this first when unsure what the host "
            "has installed or whether a session is already running."
        )
    )
    async def host_status() -> dict[str, Any]:
        await ctx.link.ensure_payload()
        status = await ctx.link.call(
            "host_status",
            {
                "tools": [t.to_spec() for t in ctx.cfg.host.tools],
                "max_concurrent_jobs": ctx.cfg.host.max_concurrent_jobs,
            },
            timeout=180.0,
        )
        status["link"] = ctx.link.describe()
        status["config"] = ctx.cfg.source
        return status

    @server.tool(
        description=(
            "Install or upgrade the agent daemon on the build server. Normally "
            "unnecessary — it is deployed automatically when missing. Pass force=true "
            "to replace a running daemon with a newer build; that restarts it and "
            "drops every open session. Detached jobs survive the restart."
        )
    )
    async def agent_ensure(force: bool = False) -> dict[str, Any]:
        return await ctx.link.ensure_payload(force=force)
