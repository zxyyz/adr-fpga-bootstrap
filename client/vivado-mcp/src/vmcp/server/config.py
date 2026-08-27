"""Client-side configuration.

The agent is deliberately config-free: whatever it needs (settings64.sh paths,
timeouts) is passed on the call. That keeps one editable file on the client
instead of two that can drift.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..common.errors import BadRequest
from ..common.models import ToolKind

DEFAULT_SSH_OPTIONS: tuple[str, ...] = (
    "BatchMode=yes",
    "ControlMaster=auto",
    "ControlPath=~/.ssh/cm-vmcp-%r@%h:%p",
    "ControlPersist=10m",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=3",
    "ConnectTimeout=15",
)

_SEARCH = (
    "vmcp.toml",
    "~/.config/vivado-mcp/config.toml",
)

EXAMPLE = """\
[host]
name      = "fpga-builder"
ssh       = "fpga-builder"
agent_dir = "~/.vivado-mcp"

[[host.tools]]
label       = "vivado-2023.2"
kind        = "vivado"
settings_sh = "/tools/Xilinx/Vivado/2023.2/settings64.sh"
default     = true
"""


@dataclass(slots=True)
class ToolCfg:
    label: str
    settings_sh: str
    kind: str = str(ToolKind.VIVADO)
    default: bool = False

    def to_spec(self) -> dict[str, str]:
        return {"label": self.label, "settings_sh": self.settings_sh, "kind": self.kind}


@dataclass(slots=True)
class HostCfg:
    name: str
    ssh: str
    agent_dir: str = "~/.vivado-mcp"
    tools: list[ToolCfg] = field(default_factory=list)
    ssh_options: tuple[str, ...] = DEFAULT_SSH_OPTIONS
    max_concurrent_jobs: int = 1
    default_jobs: int = 16
    nice: int = 10
    stall_timeout_s: float = 900.0

    @property
    def remote_payload(self) -> str:
        return f"{self.agent_dir.rstrip('/')}/bin/vmcp-agent.pyz"

    def tool(self, label: str | None) -> ToolCfg:
        if label is None:
            for tool in self.tools:
                if tool.default:
                    return tool
            if self.tools:
                return self.tools[0]
            raise BadRequest(f"host {self.name!r} has no tools configured")
        for tool in self.tools:
            if tool.label == label:
                return tool
        known = ", ".join(t.label for t in self.tools) or "none"
        raise BadRequest(f"unknown tool {label!r}; configured: {known}")


@dataclass(slots=True)
class WorkspaceCfg:
    name: str
    local: Path
    host: str
    build: str = "build"
    default: bool = False


@dataclass(slots=True)
class Config:
    host: HostCfg
    workspaces: list[WorkspaceCfg] = field(default_factory=list)
    allow_eval: bool = True
    source: str = "<defaults>"

    def workspace(self, name: str | None) -> WorkspaceCfg:
        if name is None:
            defaults = [item for item in self.workspaces if item.default]
            if len(defaults) == 1:
                return defaults[0]
            if len(self.workspaces) == 1:
                return self.workspaces[0]
            known = ", ".join(item.name for item in self.workspaces) or "none"
            raise BadRequest(
                "workspace is required unless exactly one workspace is configured; "
                f"configured: {known}"
            )
        for item in self.workspaces:
            if item.name == name:
                return item
        known = ", ".join(item.name for item in self.workspaces) or "none"
        raise BadRequest(f"unknown workspace {name!r}; configured: {known}")


def config_path() -> Path | None:
    override = os.environ.get("VMCP_CONFIG")
    if override:
        return Path(override).expanduser()
    for candidate in _SEARCH:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if path is None:
        raise BadRequest(
            "no vivado-mcp config found. Create ~/.config/vivado-mcp/config.toml, "
            f"for example:\n\n{EXAMPLE}"
        )
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    host_raw = raw.get("host")
    if not isinstance(host_raw, dict):
        raise BadRequest(f"{path}: missing [host] table")
    if "ssh" not in host_raw:
        raise BadRequest(
            f"{path}: [host] needs an 'ssh' target, e.g. user@build-server"
        )

    tools = [
        ToolCfg(
            label=t["label"],
            # This is a path on the remote build host.  Normalizing it with
            # the client's pathlib flavor corrupts POSIX paths on Windows
            # ("/opt/..." becomes "\\opt\\...").  The agent expands it on
            # the remote host, so preserve the configured spelling here.
            settings_sh=str(t["settings_sh"]),
            kind=t.get("kind", str(ToolKind.VIVADO)),
            default=bool(t.get("default", False)),
        )
        for t in host_raw.get("tools", [])
    ]
    options = host_raw.get("ssh_options")
    host = HostCfg(
        name=host_raw.get("name", host_raw["ssh"]),
        ssh=host_raw["ssh"],
        agent_dir=host_raw.get("agent_dir", "~/.vivado-mcp"),
        tools=tools,
        ssh_options=tuple(options) if options else DEFAULT_SSH_OPTIONS,
        max_concurrent_jobs=int(host_raw.get("max_concurrent_jobs", 1)),
        default_jobs=int(host_raw.get("default_jobs", 16)),
        nice=int(host_raw.get("nice", 10)),
        stall_timeout_s=float(host_raw.get("stall_timeout_s", 900.0)),
    )
    security = raw.get("security", {})
    workspaces: list[WorkspaceCfg] = []
    for item in raw.get("workspace", []):
        name = str(item.get("name", ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise BadRequest(f"{path}: invalid workspace name {name!r}")
        host_name = str(item.get("host", host.name))
        if host_name != host.name:
            raise BadRequest(
                f"{path}: workspace {name!r} selects unknown host {host_name!r}"
            )
        build = str(item.get("build", "build")).strip("/")
        if not build or any(part in {"", ".", ".."} for part in build.split("/")):
            raise BadRequest(f"{path}: workspace {name!r} has invalid build")
        local = item.get("local")
        if not local:
            raise BadRequest(f"{path}: workspace {name!r} needs 'local'")
        workspaces.append(
            WorkspaceCfg(
                name=name,
                local=Path(str(local)).expanduser().resolve(),
                host=host_name,
                build=build,
                default=bool(item.get("default", False)),
            )
        )
    return Config(
        host=host,
        workspaces=workspaces,
        allow_eval=bool(security.get("allow_eval", True)),
        source=str(path),
    )
