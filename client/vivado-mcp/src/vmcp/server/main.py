"""MCP server entry point (``vmcp-mcp``).

Runs on the client over MCP stdio and holds no durable state. Everything
that matters lives on the build server, so restarting this process never loses
a session or running build.
"""

from __future__ import annotations

import logging
import os
import sys

from mcp.server.mcpserver import MCPServer

from .. import __version__
from ..common.errors import VmcpError
from .context import Ctx
from .tools import build, host, project, report, session, workspace

INSTRUCTIONS = """\
Drives Vivado / Vitis on a remote Linux build server over ssh.

Workflow: call host_status once to see what is installed, session_open to start a
tool interpreter, then tcl_eval to drive it. Keep using the same session_id — the
interpreter holds the opened project or checkpoint, which is what makes repeated
commands fast. Use tcl_help when unsure of a command's syntax.

Long-running work (synthesis, implementation, bitstream) uses detached build
jobs. Use job_wait or launch `vmcp wait <job_id>` in the background instead of
polling. Jobs and their event logs survive SSH and daemon restarts.

For configured workspaces, call sync_status/sync_push before project tools or a
build. All project and file paths are workspace-relative; build/... addresses
the remote build tree while other paths address the synced source worktree.
"""


def build_server() -> MCPServer:
    server = MCPServer(
        name="vivado-mcp",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    ctx = Ctx.create()
    host.register(server, ctx)
    session.register(server, ctx)
    workspace.register(server, ctx)
    project.register(server, ctx)
    build.register(server, ctx)
    report.register(server, ctx)
    return server


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("VMCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        server = build_server()
    except VmcpError as exc:
        print(f"vivado-mcp: {exc}", file=sys.stderr)
        return 2
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
