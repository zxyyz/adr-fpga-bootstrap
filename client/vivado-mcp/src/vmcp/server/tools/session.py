"""Session lifecycle and the Tcl escape hatch.

Vivado's Tcl surface is far too large to wrap command by command, so ``tcl_eval``
is a first-class tool rather than a workaround.  What it will *not* do is run a
command that blocks the interpreter: that would make the session unobservable
and uninterruptible for hours. Those commands belong to detached build jobs.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.server.mcpserver import MCPServer

from ...common.errors import BadRequest
from ..context import Ctx, clamp

#: Commands that end the interpreter. Never allowed — they would destroy the
#: session out from under whoever else is using it.
_FATAL = re.compile(r"(?<![\w:.-])(exit|quit|start_gui)(?![\w:.-])")

#: Long-running commands. They would pin the interpreter for minutes to hours
#: with no way to query progress or cancel.
_BLOCKING = re.compile(
    r"(?<![\w:.-])("
    r"synth_design|opt_design|place_design|phys_opt_design|power_opt_design"
    r"|route_design|write_bitstream|write_device_image"
    r"|launch_runs|wait_on_run|wait_on_runs|launch_simulation"
    r"|csynth_design|cosim_design|export_design"
    r")(?![\w:.-])"
)


def check_script(script: str, allow_blocking: bool) -> None:
    if not script.strip():
        raise BadRequest("script is empty")
    fatal = _FATAL.search(script)
    if fatal:
        raise BadRequest(
            f"{fatal.group(1)!r} would terminate the session; use session_close instead"
        )
    if allow_blocking:
        return
    blocking = _BLOCKING.search(script)
    if blocking:
        raise BadRequest(
            f"{blocking.group(1)!r} blocks the Tcl interpreter for the whole run, "
            f"which makes the session unobservable and uninterruptible. Use a build "
            f"job instead. If you really want to block this session (short runs on "
            f"tiny designs only), pass allow_blocking=true and raise timeout_s."
        )


def register(server: MCPServer, ctx: Ctx) -> None:
    @server.tool(
        description=(
            "Start a long-lived tool interpreter on the build server and return its "
            "session_id. Reuse one session across many commands: it keeps the opened "
            "project or checkpoint in memory, which is the expensive part. 'tool' "
            "names an entry from the configured tool table (default: the one marked "
            "default). 'cwd' is where the tool runs; defaults to the agent home."
        )
    )
    async def session_open(
        tool: str | None = None, cwd: str | None = None
    ) -> dict[str, Any]:
        await ctx.link.ensure_payload()
        cfg = ctx.cfg.host.tool(tool)
        return await ctx.link.call(
            "session_open",
            {
                "kind": cfg.kind,
                "label": cfg.label,
                "settings_sh": cfg.settings_sh,
                "cwd": cwd,
            },
            timeout=300.0,
        )

    @server.tool(
        description=(
            "List the tool sessions currently alive on the build server, with their "
            "state (idle/busy/dead), idle time, command count and log path."
        )
    )
    async def session_list() -> dict[str, Any]:
        sessions = await ctx.link.call("session_list", timeout=60.0)
        return {"sessions": sessions, "count": len(sessions)}

    @server.tool(
        description=(
            "Shut down a tool session and free its memory (a Vivado interpreter holds "
            "about a gigabyte). Anything not written to disk is lost."
        )
    )
    async def session_close(session_id: str) -> dict[str, Any]:
        return await ctx.link.call(
            "session_close", {"session_id": session_id}, timeout=60.0
        )

    @server.tool(
        description=(
            "Run a Tcl script in an open session and return its result plus the tool "
            "output it produced. This is the general-purpose way to drive Vivado: "
            "open_project, get_property, report_* to a file, IP and block-design "
            "commands, anything. Commands that block the interpreter for a long time "
            "(synth_design, route_design, launch_runs, ...) are refused; use a build "
            "job for those. rc=0 means success; rc=1 means the script raised, and "
            "errorinfo carries the Tcl traceback."
        )
    )
    async def tcl_eval(
        session_id: str,
        script: str,
        timeout_s: float = 120.0,
        allow_blocking: bool = False,
    ) -> dict[str, Any]:
        if not ctx.cfg.allow_eval:
            raise BadRequest(
                "tcl_eval is disabled by security.allow_eval in the config"
            )
        check_script(script, allow_blocking)
        result = await ctx.link.call(
            "session_eval",
            {"session_id": session_id, "script": script, "timeout_s": timeout_s},
            timeout=timeout_s + 30.0,
        )
        return _shape_eval(result)

    @server.tool(
        description=(
            "Look up a tool command's own help text (Vivado 'help <command>'), "
            "including its full option list. Use this instead of guessing syntax: it "
            "reflects the exact tool version installed on the build server. Needs an "
            "open session."
        )
    )
    async def tcl_help(session_id: str, command: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z_][\w:]*", command):
            raise BadRequest(f"not a command name: {command!r}")
        result = await ctx.link.call(
            "session_eval",
            {"session_id": session_id, "script": f"help {command}", "timeout_s": 60.0},
            timeout=90.0,
        )
        shaped = _shape_eval(result)
        # Vivado's `help` prints to the log and returns nothing useful.
        if shaped["rc"] == 0 and not shaped["result"]:
            shaped["result"] = shaped.pop("log", "")
            shaped.pop("log_truncated", None)
        return shaped


def _shape_eval(raw: dict[str, Any]) -> dict[str, Any]:
    result, result_truncated = clamp(str(raw.get("result", "")))
    log_text, log_truncated = clamp(str(raw.get("log", "")))
    shaped: dict[str, Any] = {
        "rc": raw.get("rc"),
        "ok": raw.get("rc") == 0,
        "result": result,
        "log": log_text,
        "elapsed_s": raw.get("elapsed_s"),
    }
    if result_truncated:
        shaped["result_truncated"] = True
    if log_truncated:
        shaped["log_truncated"] = True
    if raw.get("log_lines_dropped"):
        shaped["log_lines_dropped"] = raw["log_lines_dropped"]
    if raw.get("errorinfo"):
        shaped["errorinfo"], _ = clamp(str(raw["errorinfo"]), 4000)
    return shaped
