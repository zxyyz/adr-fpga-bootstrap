"""Structured Vivado project and source management tools."""

# MCP registration is intentionally one flat function: each nested function is
# independently exposed by the SDK, and splitting it adds indirection only.
# pylint: disable=too-many-statements

from __future__ import annotations

import base64
import re
from pathlib import PurePosixPath
from typing import Any

from mcp.server.mcpserver import MCPServer

from ...common.errors import BadRequest
from ...common.tcl import b64encode_utf8
from ..context import Ctx
from ..workspace import validate_relative
from .session import _shape_eval

_PROPERTY = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*\Z")
_RUN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")


def _scalar(name: str, value: str) -> str:
    return (
        f"set {name} [encoding convertfrom utf-8 "
        f"[binary decode base64 {{{b64encode_utf8(value)}}}]]"
    )


def _list(name: str, values: list[str]) -> list[str]:
    lines = [f"set {name} [list]"]
    for value in values:
        lines += [_scalar("vmcp_item", value), f"lappend {name} $vmcp_item"]
    return lines


def _workspace_params(cfg, **extra: Any) -> dict[str, Any]:
    return {
        "workspace": cfg.name,
        "build": cfg.build,
        **extra,
    }


async def _eval(ctx: Ctx, session_id: str, script: str) -> dict[str, Any]:
    raw = await ctx.link.call(
        "session_eval",
        {"session_id": session_id, "script": script, "timeout_s": 120.0},
        timeout=150.0,
    )
    return _shape_eval(raw)


def _require_ok(result: dict[str, Any], action: str) -> dict[str, Any]:
    if result.get("ok"):
        return result
    detail = result.get("errorinfo") or result.get("result") or result.get("log")
    raise BadRequest(f"Vivado could not {action}: {detail}")


def _info_script() -> str:
    return """\
proc vmcp_enc {value} {
    return [binary encode base64 -maxlen 0 [encoding convertto utf-8 $value]]
}
set vmcp_lines [list]
proc vmcp_emit {kind args} {
    global vmcp_lines
    set fields [list $kind]
    foreach value $args { lappend fields [vmcp_enc $value] }
    lappend vmcp_lines [join $fields |]
}
set project [current_project]
set fileset [current_fileset]
vmcp_emit meta \
    [get_property NAME $project] \
    [get_property PART $project] \
    [get_property BOARD_PART $project] \
    [get_property TARGET_LANGUAGE $project] \
    [get_property TOP $fileset]
foreach file [get_files -quiet] {
    vmcp_emit source \
        [get_property NAME $file] \
        [get_property FILE_TYPE $file] \
        [get_property LIBRARY $file]
}
foreach run [get_runs -quiet] {
    vmcp_emit run \
        [get_property NAME $run] \
        [get_property STATUS $run] \
        [get_property PROGRESS $run] \
        [get_property STRATEGY $run]
}
foreach ip [get_ips -quiet] {
    vmcp_emit ip [get_property NAME $ip] [get_property IPDEF $ip]
}
return [join $vmcp_lines "\\n"]
"""


def _parse_info(raw: str) -> dict[str, Any]:
    result: dict[str, Any] = {"sources": [], "runs": [], "ips": []}
    for line in raw.splitlines():
        fields = line.split("|")
        if len(fields) < 2:
            continue
        kind = fields[0]
        try:
            values = [
                base64.b64decode(item, validate=True).decode("utf-8")
                for item in fields[1:]
            ]
        except (ValueError, UnicodeDecodeError):
            continue
        if kind == "meta" and len(values) >= 5:
            result.update(
                {
                    "name": values[0],
                    "part": values[1],
                    "board": values[2] or None,
                    "target_language": values[3],
                    "top": values[4] or None,
                }
            )
        elif kind == "source" and len(values) >= 3:
            result["sources"].append(
                {"path": values[0], "type": values[1], "library": values[2]}
            )
        elif kind == "run" and len(values) >= 4:
            result["runs"].append(
                {
                    "name": values[0],
                    "status": values[1],
                    "progress": values[2],
                    "strategy": values[3],
                }
            )
        elif kind == "ip" and len(values) >= 2:
            result["ips"].append({"name": values[0], "vlnv": values[1]})
    result["source_count"] = len(result["sources"])
    result["ip_count"] = len(result["ips"])
    return result


async def _project_info(ctx: Ctx, session_id: str) -> dict[str, Any]:
    result = _require_ok(
        await _eval(ctx, session_id, _info_script()), "inspect project"
    )
    return _parse_info(str(result.get("result", "")))


async def _relativize_info(ctx: Ctx, cfg, info: dict[str, Any]) -> dict[str, Any]:
    status = await ctx.link.call(
        "workspace_status", _workspace_params(cfg), timeout=60.0
    )
    work = PurePosixPath(status["work"])
    build = PurePosixPath(status["build_path"])
    for source in info.get("sources", []):
        path = PurePosixPath(source["path"])
        if path.is_relative_to(work):
            source["path"] = path.relative_to(work).as_posix()
        elif path.is_relative_to(build):
            suffix = path.relative_to(build).as_posix()
            source["path"] = f"{cfg.build}/{suffix}"
    return info


def register(
    server: MCPServer, ctx: Ctx
) -> None:  # pylint: disable=too-many-statements
    @server.tool(
        description=(
            "Create a Vivado .xpr under the workspace (normally build/name/name.xpr), "
            "open it in a new long-lived session, and hold its single-writer lock. "
            "Exactly one of part or board is required."
        )
    )
    async def project_create(
        path: str,
        part: str | None = None,
        board: str | None = None,
        top: str | None = None,
        target_language: str = "Verilog",
        workspace: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        if bool(part) == bool(board):
            raise BadRequest("provide exactly one of part or board")
        if target_language.lower() not in {"verilog", "vhdl"}:
            raise BadRequest("target_language must be Verilog or VHDL")
        relative = validate_relative(path)
        if PurePosixPath(relative).suffix.lower() != ".xpr":
            raise BadRequest("project path must end in .xpr")
        cfg = ctx.cfg.workspace(workspace)
        await ctx.link.ensure_payload()
        await ctx.link.call("workspace_prepare", _workspace_params(cfg), timeout=120.0)
        resolved = await ctx.link.call(
            "workspace_resolve", _workspace_params(cfg, path=relative), timeout=60.0
        )
        if resolved["exists"]:
            raise BadRequest(f"project already exists: {relative}")
        parent = PurePosixPath(relative).parent.as_posix()
        await ctx.link.call(
            "workspace_mkdir", _workspace_params(cfg, path=parent), timeout=60.0
        )
        tool_cfg = ctx.cfg.host.tool(tool)
        session = await ctx.link.call(
            "session_open",
            {
                "kind": tool_cfg.kind,
                "label": tool_cfg.label,
                "settings_sh": tool_cfg.settings_sh,
                "cwd": str(PurePosixPath(resolved["remote"]).parent),
            },
            timeout=300.0,
        )
        sid = session["session_id"]
        try:
            await ctx.link.call(
                "project_claim",
                _workspace_params(cfg, session_id=sid, project=relative),
                timeout=60.0,
            )
            lines = [
                _scalar("vmcp_name", PurePosixPath(relative).stem),
                _scalar("vmcp_dir", str(PurePosixPath(resolved["remote"]).parent)),
            ]
            if part:
                lines += [
                    _scalar("vmcp_part", part),
                    "create_project $vmcp_name $vmcp_dir -part $vmcp_part",
                ]
            else:
                lines += [
                    "create_project $vmcp_name $vmcp_dir",
                    _scalar("vmcp_board", str(board)),
                    "set_property BOARD_PART $vmcp_board [current_project]",
                ]
            lines += [
                _scalar("vmcp_language", target_language),
                "set_property TARGET_LANGUAGE $vmcp_language [current_project]",
            ]
            if top:
                lines += [
                    _scalar("vmcp_top", top),
                    "set_property TOP $vmcp_top [current_fileset]",
                ]
            lines += ["return [get_property NAME [current_project]]"]
            _require_ok(await _eval(ctx, sid, "\n".join(lines)), "create project")
        except BaseException:
            await ctx.link.call("project_release", {"session_id": sid}, timeout=30.0)
            await ctx.link.call("session_close", {"session_id": sid}, timeout=60.0)
            raise
        return {
            "workspace": cfg.name,
            "path": relative,
            "session_id": sid,
            "project": await _relativize_info(ctx, cfg, await _project_info(ctx, sid)),
        }

    @server.tool(
        description="Open an existing workspace-relative .xpr in a locked session."
    )
    async def project_open(
        path: str, workspace: str | None = None, tool: str | None = None
    ) -> dict[str, Any]:
        relative = validate_relative(path)
        cfg = ctx.cfg.workspace(workspace)
        await ctx.link.ensure_payload()
        resolved = await ctx.link.call(
            "workspace_resolve",
            _workspace_params(cfg, path=relative, must_exist=True),
            timeout=60.0,
        )
        tool_cfg = ctx.cfg.host.tool(tool)
        session = await ctx.link.call(
            "session_open",
            {
                "kind": tool_cfg.kind,
                "label": tool_cfg.label,
                "settings_sh": tool_cfg.settings_sh,
                "cwd": str(PurePosixPath(resolved["remote"]).parent),
            },
            timeout=300.0,
        )
        sid = session["session_id"]
        try:
            await ctx.link.call(
                "project_claim",
                _workspace_params(cfg, session_id=sid, project=relative),
                timeout=60.0,
            )
            script = "\n".join(
                [
                    _scalar("vmcp_project", resolved["remote"]),
                    "open_project $vmcp_project",
                ]
            )
            _require_ok(await _eval(ctx, sid, script), "open project")
        except BaseException:
            await ctx.link.call("project_release", {"session_id": sid}, timeout=30.0)
            await ctx.link.call("session_close", {"session_id": sid}, timeout=60.0)
            raise
        return {
            "workspace": cfg.name,
            "path": relative,
            "session_id": sid,
            "project": await _relativize_info(ctx, cfg, await _project_info(ctx, sid)),
        }

    @server.tool(
        description="Close the project in a session and release its single-writer lock."
    )
    async def project_close(session_id: str) -> dict[str, Any]:
        _require_ok(await _eval(ctx, session_id, "close_project"), "close project")
        await ctx.link.call("project_release", {"session_id": session_id}, timeout=60.0)
        return {"closed": True, "session_id": session_id}

    @server.tool(
        description=(
            "Return part, board, top, source inventory, run status, IP inventory and "
            "residual lock information for the project open in a session."
        )
    )
    async def project_info(
        session_id: str,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        cfg = ctx.cfg.workspace(workspace)
        binding = await ctx.link.call(
            "project_binding",
            _workspace_params(cfg, session_id=session_id),
            timeout=60.0,
        )
        info = await _relativize_info(ctx, cfg, await _project_info(ctx, session_id))
        locks = await ctx.link.call(
            "project_locks",
            _workspace_params(cfg, project=binding["project"]),
            timeout=60.0,
        )
        info["path"] = binding["project"]
        info["locks"] = locks
        return info

    @server.tool(
        description="Add workspace-relative HDL, constraint or data files to the open project."
    )
    async def sources_add(
        session_id: str,
        files: list[str],
        workspace: str | None = None,
        type: str = "auto",  # pylint: disable=redefined-builtin
        library: str | None = None,
        scoped_to: str | None = None,
    ) -> dict[str, Any]:
        if not files:
            raise BadRequest("sources_add needs at least one file")
        cfg = ctx.cfg.workspace(workspace)
        remote = []
        relative = []
        for item in files:
            value = validate_relative(item)
            got = await ctx.link.call(
                "workspace_resolve",
                _workspace_params(cfg, path=value, must_exist=True),
                timeout=60.0,
            )
            if not got["is_file"]:
                raise BadRequest(f"source is not a file: {value}")
            relative.append(value)
            remote.append(got["remote"])
        lines = _list("vmcp_files", remote) + ["add_files -norecurse $vmcp_files"]
        if type != "auto" or library or scoped_to:
            lines += [
                "foreach vmcp_file $vmcp_files {",
                "set vmcp_obj [get_files -quiet [list $vmcp_file]]",
            ]
            if type != "auto":
                lines += [
                    _scalar("vmcp_type", type),
                    "set_property FILE_TYPE $vmcp_type $vmcp_obj",
                ]
            if library:
                lines += [
                    _scalar("vmcp_library", library),
                    "set_property LIBRARY $vmcp_library $vmcp_obj",
                ]
            if scoped_to:
                lines += [
                    _scalar("vmcp_scope", scoped_to),
                    "set_property SCOPED_TO_REF $vmcp_scope $vmcp_obj",
                ]
            lines.append("}")
        _require_ok(await _eval(ctx, session_id, "\n".join(lines)), "add sources")
        return {"added": relative, "count": len(relative)}

    @server.tool(
        description="Remove files from the open project without deleting them from disk."
    )
    async def sources_remove(
        session_id: str, files: list[str], workspace: str | None = None
    ) -> dict[str, Any]:
        cfg = ctx.cfg.workspace(workspace)
        remote = []
        relative = []
        for item in files:
            value = validate_relative(item)
            got = await ctx.link.call(
                "workspace_resolve", _workspace_params(cfg, path=value), timeout=60.0
            )
            relative.append(value)
            remote.append(got["remote"])
        lines = _list("vmcp_files", remote) + [
            "foreach vmcp_file $vmcp_files { remove_files [get_files -quiet [list $vmcp_file]] }",
        ]
        _require_ok(await _eval(ctx, session_id, "\n".join(lines)), "remove sources")
        return {"removed": relative, "count": len(relative)}

    @server.tool(
        description="List files currently registered in the open Vivado project."
    )
    async def sources_list(
        session_id: str, workspace: str | None = None
    ) -> dict[str, Any]:
        cfg = ctx.cfg.workspace(workspace)
        info = await _relativize_info(ctx, cfg, await _project_info(ctx, session_id))
        return {"sources": info["sources"], "count": info["source_count"]}

    @server.tool(description="Set the top module/entity on the current source fileset.")
    async def set_top(session_id: str, top: str) -> dict[str, Any]:
        script = "\n".join(
            [
                _scalar("vmcp_top", top),
                "set_property TOP $vmcp_top [current_fileset]",
            ]
        )
        _require_ok(await _eval(ctx, session_id, script), "set top")
        return {"top": top}

    @server.tool(
        description="Set Vivado generic/parameter overrides on the current fileset."
    )
    async def set_generics(session_id: str, generics: dict[str, str]) -> dict[str, Any]:
        value = " ".join(f"{key}={item}" for key, item in generics.items())
        script = "\n".join(
            [
                _scalar("vmcp_generics", value),
                "set_property GENERIC $vmcp_generics [current_fileset]",
            ]
        )
        _require_ok(await _eval(ctx, session_id, script), "set generics")
        return {"generics": generics}

    @server.tool(
        description="Append workspace-relative Verilog/SystemVerilog include directories."
    )
    async def add_include_dirs(
        session_id: str, directories: list[str], workspace: str | None = None
    ) -> dict[str, Any]:
        cfg = ctx.cfg.workspace(workspace)
        remote = []
        for item in directories:
            got = await ctx.link.call(
                "workspace_resolve",
                _workspace_params(cfg, path=validate_relative(item), must_exist=True),
                timeout=60.0,
            )
            if not got["is_dir"]:
                raise BadRequest(f"include path is not a directory: {item}")
            remote.append(got["remote"])
        lines = _list("vmcp_dirs", remote) + [
            "set vmcp_old [get_property INCLUDE_DIRS [current_fileset]]",
            "set_property INCLUDE_DIRS [concat $vmcp_old $vmcp_dirs] [current_fileset]",
        ]
        _require_ok(await _eval(ctx, session_id, "\n".join(lines)), "add include dirs")
        return {"added": directories}

    @server.tool(
        description="Set the synthesis or implementation strategy for a named run."
    )
    async def set_run_strategy(
        session_id: str, run: str, strategy: str
    ) -> dict[str, Any]:
        if not _RUN.fullmatch(run):
            raise BadRequest(f"invalid run name: {run!r}")
        script = "\n".join(
            [
                _scalar("vmcp_run", run),
                _scalar("vmcp_strategy", strategy),
                "set_property STRATEGY $vmcp_strategy [get_runs $vmcp_run]",
            ]
        )
        _require_ok(await _eval(ctx, session_id, script), "set run strategy")
        return {"run": run, "strategy": strategy}

    @server.tool(
        description=(
            "Set one property on a typed Vivado object selector: project, fileset, "
            "run:<name>, ip:<name>, or file:<workspace-relative-path>."
        )
    )
    async def set_property(
        session_id: str,
        object: str,  # pylint: disable=redefined-builtin
        name: str,
        value: str,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        if not _PROPERTY.fullmatch(name):
            raise BadRequest(f"invalid property name: {name!r}")
        if name.upper().endswith((".TCL.PRE", ".TCL.POST")):
            cfg = ctx.cfg.workspace(workspace)
            got = await ctx.link.call(
                "workspace_resolve",
                _workspace_params(cfg, path=validate_relative(value), must_exist=True),
                timeout=60.0,
            )
            value = got["remote"]
        lines = [_scalar("vmcp_name", name), _scalar("vmcp_value", value)]
        if object == "project":
            lines.append("set vmcp_object [current_project]")
        elif object == "fileset":
            lines.append("set vmcp_object [current_fileset]")
        elif object.startswith("run:"):
            lines += [
                _scalar("vmcp_selector", object[4:]),
                "set vmcp_object [get_runs $vmcp_selector]",
            ]
        elif object.startswith("ip:"):
            lines += [
                _scalar("vmcp_selector", object[3:]),
                "set vmcp_object [get_ips $vmcp_selector]",
            ]
        elif object.startswith("file:"):
            cfg = ctx.cfg.workspace(workspace)
            got = await ctx.link.call(
                "workspace_resolve",
                _workspace_params(
                    cfg, path=validate_relative(object[5:]), must_exist=True
                ),
                timeout=60.0,
            )
            lines += [
                _scalar("vmcp_selector", got["remote"]),
                "set vmcp_object [get_files -quiet [list $vmcp_selector]]",
            ]
        else:
            raise BadRequest(
                "object must be project, fileset, run:<name>, ip:<name>, or file:<path>"
            )
        lines += ["set_property $vmcp_name $vmcp_value $vmcp_object"]
        _require_ok(await _eval(ctx, session_id, "\n".join(lines)), "set property")
        return {"object": object, "name": name, "value": value}

    @server.tool(description="Create and optionally configure a Vivado IP instance.")
    async def ip_create(
        session_id: str,
        module_name: str,
        vlnv: str,
        config: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        lines = [
            _scalar("vmcp_module", module_name),
            _scalar("vmcp_vlnv", vlnv),
            "create_ip -vlnv $vmcp_vlnv -module_name $vmcp_module",
        ]
        if config:
            pairs: list[str] = []
            for key, value in config.items():
                prop = key if key.startswith("CONFIG.") else f"CONFIG.{key}"
                if not _PROPERTY.fullmatch(prop):
                    raise BadRequest(f"invalid IP config property: {key!r}")
                pairs += [prop, str(value)]
            lines += _list("vmcp_config", pairs)
            lines.append("set_property -dict $vmcp_config [get_ips $vmcp_module]")
        _require_ok(await _eval(ctx, session_id, "\n".join(lines)), "create IP")
        return {"module_name": module_name, "vlnv": vlnv}

    @server.tool(
        description="Upgrade selected IP instances to the current catalog version."
    )
    async def ip_upgrade(session_id: str, ips: list[str]) -> dict[str, Any]:
        lines = _list("vmcp_ips", ips) + [
            "foreach vmcp_ip $vmcp_ips { upgrade_ip [get_ips $vmcp_ip] }",
        ]
        _require_ok(await _eval(ctx, session_id, "\n".join(lines)), "upgrade IP")
        return {"upgraded": ips}

    @server.tool(description="Generate all targets for selected IP instances.")
    async def ip_generate(session_id: str, ips: list[str]) -> dict[str, Any]:
        lines = _list("vmcp_ips", ips) + [
            "foreach vmcp_ip $vmcp_ips { generate_target all [get_ips $vmcp_ip] }",
        ]
        _require_ok(await _eval(ctx, session_id, "\n".join(lines)), "generate IP")
        return {"generated": ips}

    @server.tool(
        description=(
            "Run a detached non-project Vivado flow from workspace-relative HDL "
            "and XDC files. Produces checkpoints (and optionally a bitstream) under "
            "build/flows/<job_id> and returns immediately with the durable job_id."
        )
    )
    async def flow_run(
        sources: list[str],
        part: str,
        top: str,
        xdc: list[str] | None = None,
        target: str = "synth",
        workspace: str | None = None,
        tool: str | None = None,
        idempotency_key: str | None = None,
        timeout_s: float = 0.0,
    ) -> dict[str, Any]:
        if target not in {"synth", "impl", "bitstream"}:
            raise BadRequest("target must be synth, impl, or bitstream")
        if not sources:
            raise BadRequest("flow_run needs at least one source")
        cfg = ctx.cfg.workspace(workspace)
        tool_cfg = ctx.cfg.host.tool(tool)
        await ctx.link.ensure_payload()
        return await ctx.link.call(
            "flow_submit",
            _workspace_params(
                cfg,
                sources=[validate_relative(item) for item in sources],
                xdc=[validate_relative(item) for item in (xdc or [])],
                part=part,
                top=top,
                target=target,
                idempotency_key=idempotency_key,
                label=tool_cfg.label,
                settings_sh=tool_cfg.settings_sh,
                max_concurrent_jobs=ctx.cfg.host.max_concurrent_jobs,
                nice=ctx.cfg.host.nice,
                timeout_s=timeout_s,
                stall_timeout_s=ctx.cfg.host.stall_timeout_s,
            ),
            timeout=300.0,
        )

    @server.tool(
        description=(
            "Remove a residual Vivado .lck file after a crash. Refuses while any "
            "live session owns the project."
        )
    )
    async def project_unlock(path: str, workspace: str | None = None) -> dict[str, Any]:
        cfg = ctx.cfg.workspace(workspace)
        return await ctx.link.call(
            "project_unlock",
            _workspace_params(cfg, project=validate_relative(path)),
            timeout=60.0,
        )
