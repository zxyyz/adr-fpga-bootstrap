"""Compact, normalized Vivado report tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ...common.errors import BadRequest
from ..context import Ctx
from ..workspace import validate_relative


def _source_params(
    ctx: Ctx,
    source: str,
    workspace: str | None,
    tool: str | None,
) -> dict[str, Any]:
    cfg = ctx.cfg.host.tool(tool)
    params: dict[str, Any] = {
        "source": source,
        "label": cfg.label,
        "settings_sh": cfg.settings_sh,
    }
    if not source.startswith("j_"):
        workspace_cfg = ctx.cfg.workspace(workspace)
        params.update(
            {
                "source": validate_relative(source),
                "workspace": workspace_cfg.name,
                "build": workspace_cfg.build,
            }
        )
    return params


async def _get(
    ctx: Ctx,
    source: str,
    kind: str,
    args: dict[str, Any],
    workspace: str | None,
    tool: str | None,
) -> dict[str, Any]:
    await ctx.link.ensure_payload()
    return await ctx.link.call(
        "report_get",
        {
            **_source_params(ctx, source, workspace, tool),
            "kind": kind,
            "args": args,
        },
        timeout=660.0,
    )


def register(
    server: MCPServer, ctx: Ctx
) -> None:  # pylint: disable=too-many-statements
    @server.tool(
        description=(
            "Parse a job or workspace-relative DCP utilization report into stable "
            "LUT/LUTRAM/FF/BRAM/URAM/DSP/IO/BUFG used, available and percent fields. "
            "Zero-use resources are omitted. Missing reports are generated from the DCP."
        )
    )
    async def report_utilization(
        source: str,
        hierarchical: bool = False,
        cells: list[str] | None = None,
        workspace: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        return await _get(
            ctx,
            source,
            "utilization",
            {"hierarchical": hierarchical, "cells": cells or []},
            workspace,
            tool,
        )

    @server.tool(
        description=(
            "Return normalized WNS/TNS/WHS/THS/pulse-width metrics, per-clock-domain "
            "endpoint counts and compact worst-path summaries from a job or DCP."
        )
    )
    async def report_timing_summary(
        source: str,
        max_paths: int = 5,
        workspace: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        return await _get(
            ctx,
            source,
            "timing",
            {"max_paths": max_paths},
            workspace,
            tool,
        )

    @server.tool(
        description=(
            "Inspect worst timing paths, optionally constrained by endpoint patterns. "
            "summary returns compressed path metrics; full is limited to nworst=1."
        )
    )
    async def report_timing_paths(
        source: str,
        from_endpoint: str | None = None,
        to_endpoint: str | None = None,
        through: str | None = None,
        nworst: int = 5,
        delay_type: str = "max",
        detail: str = "summary",
        workspace: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        return await _get(
            ctx,
            source,
            "timing_paths",
            {
                "from_endpoint": from_endpoint,
                "to_endpoint": to_endpoint,
                "through": through,
                "nworst": nworst,
                "delay_type": delay_type,
                "detail": detail,
            },
            workspace,
            tool,
        )

    @server.tool(
        description=(
            "Return clock periods, target frequencies, achieved frequencies and "
            "per-domain timing metrics from a job or DCP."
        )
    )
    async def report_clocks(
        source: str,
        workspace: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        return await _get(ctx, source, "clocks", {}, workspace, tool)

    @server.tool(
        description="Group DRC violations by rule with severity, count and one example."
    )
    async def report_drc(
        source: str,
        workspace: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        return await _get(ctx, source, "drc", {}, workspace, tool)

    @server.tool(
        description="Group methodology findings by rule without returning the full report."
    )
    async def report_methodology(
        source: str,
        workspace: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        return await _get(ctx, source, "methodology", {}, workspace, tool)

    @server.tool(
        description="Group clock-domain-crossing findings by rule and severity."
    )
    async def report_cdc(
        source: str,
        workspace: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        return await _get(ctx, source, "cdc", {}, workspace, tool)

    @server.tool(
        description="Return normalized total, dynamic, static and component power data."
    )
    async def report_power(
        source: str,
        workspace: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        return await _get(ctx, source, "power", {}, workspace, tool)

    @server.tool(
        description=(
            "Compare two job IDs or DCP paths using normalized utilization or timing "
            "metrics, returning only semantic deltas."
        )
    )
    async def report_diff(
        a: str,
        b: str,
        kind: str,
        workspace: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        await ctx.link.ensure_payload()
        a_params = _source_params(ctx, a, workspace, tool)
        b_params = _source_params(ctx, b, workspace, tool)
        if a_params.get("workspace") != b_params.get("workspace"):
            raise BadRequest("report_diff sources must use the same workspace")
        return await ctx.link.call(
            "report_diff",
            {
                "a": a_params["source"],
                "b": b_params["source"],
                "kind": kind,
                "workspace": a_params.get("workspace"),
                "build": a_params.get("build"),
                "label": a_params["label"],
                "settings_sh": a_params["settings_sh"],
            },
            timeout=1320.0,
        )
