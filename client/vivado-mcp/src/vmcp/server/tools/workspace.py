"""Workspace sync and bounded remote file tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from ...common.errors import BadRequest
from ..context import Ctx
from ..workspace import GitWorkspace, rsync_pull, validate_relative


def _params(cfg, **extra: Any) -> dict[str, Any]:
    return {
        "workspace": cfg.name,
        "build": cfg.build,
        **extra,
    }


def register(server: MCPServer, ctx: Ctx) -> None:
    @server.tool(
        description=(
            "Show local Git HEAD/dirty files and the exact commit currently checked "
            "out on the build server. Paths are workspace-relative."
        )
    )
    async def sync_status(workspace: str | None = None) -> dict[str, Any]:
        cfg = ctx.cfg.workspace(workspace)
        await ctx.link.ensure_payload()
        local = await GitWorkspace(cfg, ctx.cfg.host).status()
        remote = await ctx.link.call("workspace_status", _params(cfg), timeout=60.0)
        return {"workspace": cfg.name, "local": local, "remote": remote}

    @server.tool(
        description=(
            "Synchronize the local Git worktree to the build host. Dirty and staged "
            "changes are captured in a synthetic commit using a temporary index; "
            "the user's HEAD, branch and index are never changed. Optional paths "
            "limit which dirty changes overlay HEAD. dry_run reports changes only."
        )
    )
    async def sync_push(
        workspace: str | None = None,
        paths: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        cfg = ctx.cfg.workspace(workspace)
        git = GitWorkspace(cfg, ctx.cfg.host)
        local = await git.status()
        selected = [validate_relative(item) for item in paths] if paths else None
        if dry_run:
            return {
                "workspace": cfg.name,
                "dry_run": True,
                "selected": selected,
                "local": local,
            }
        await ctx.link.ensure_payload()
        prepared = await ctx.link.call("workspace_prepare", _params(cfg), timeout=120.0)
        staged = await git.staging_commit(selected)
        await git.push(staged["commit"], prepared["repo"])
        remote = await ctx.link.call(
            "workspace_activate",
            _params(cfg, commit=staged["commit"]),
            timeout=180.0,
        )
        return {
            "workspace": cfg.name,
            "commit": staged["commit"],
            "parent": staged["parent"],
            "dirty": local["dirty"],
            "changed": local["changed"],
            "remote": {
                "commit": remote["commit"],
                "updated_at": remote["updated_at"],
            },
        }

    @server.tool(
        description=(
            "Copy selected workspace-relative files or directories from the build "
            "host to the local workspace with rsync. This never uses --delete."
        )
    )
    async def sync_pull(
        paths: list[str], workspace: str | None = None
    ) -> dict[str, Any]:
        if not paths:
            raise BadRequest("sync_pull needs at least one path")
        cfg = ctx.cfg.workspace(workspace)
        await ctx.link.ensure_payload()
        pulled = []
        for value in paths:
            relative = validate_relative(value)
            resolved = await ctx.link.call(
                "workspace_resolve",
                _params(cfg, path=relative, must_exist=True),
                timeout=60.0,
            )
            destination = (cfg.local / Path(relative)).resolve()
            if not destination.is_relative_to(cfg.local):
                raise BadRequest(f"local path escapes workspace: {relative!r}")
            await rsync_pull(
                ctx.cfg.host,
                resolved["remote"],
                destination,
                is_dir=bool(resolved["is_dir"]),
            )
            pulled.append(relative)
        return {"workspace": cfg.name, "pulled": pulled, "delete": False}

    @server.tool(
        description=(
            "Read a bounded UTF-8 slice of a workspace file on the build host. "
            "Use next_offset to continue without loading a large report at once."
        )
    )
    async def file_read(
        path: str,
        workspace: str | None = None,
        offset: int = 0,
        limit: int = 12000,
    ) -> dict[str, Any]:
        cfg = ctx.cfg.workspace(workspace)
        await ctx.link.ensure_payload()
        return await ctx.link.call(
            "file_read",
            _params(cfg, path=validate_relative(path), offset=offset, limit=limit),
            timeout=60.0,
        )

    @server.tool(
        description=(
            "Regex-search text files in the remote source and build trees. Results "
            "are bounded and paths are always workspace-relative."
        )
    )
    async def file_grep(
        pattern: str,
        workspace: str | None = None,
        glob: str = "**/*",
        limit: int = 200,
    ) -> dict[str, Any]:
        cfg = ctx.cfg.workspace(workspace)
        await ctx.link.ensure_payload()
        return await ctx.link.call(
            "file_grep",
            _params(cfg, pattern=pattern, glob=glob, limit=limit),
            timeout=120.0,
        )
