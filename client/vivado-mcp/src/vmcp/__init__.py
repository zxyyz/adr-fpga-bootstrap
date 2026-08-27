"""Vivado / Vitis MCP.

Two entry points share this package:

* ``vmcp.server`` — the MCP server, runs on the client next to the MCP host.
* ``vmcp.agent``  — the daemon, runs on the remote build server.

``vmcp.common`` and ``vmcp.agent`` are shipped to the build server as a zipapp and
must therefore stay **stdlib-only**.  Only ``vmcp.server`` and ``vmcp.cli`` may
use third-party dependencies.
"""

PROTOCOL_VERSION = 4
__version__ = "0.4.0"
