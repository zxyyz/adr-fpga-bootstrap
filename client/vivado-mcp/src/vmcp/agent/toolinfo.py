"""Tool discovery and host facts.

The agent holds no configuration of its own: the client passes the tool table
(label -> settings64.sh) on every call that needs it.  That keeps config in one
place — ``~/.config/vivado-mcp/config.toml`` on the client.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import socket
from pathlib import Path

from ..common.errors import ToolNotFound
from ..common.models import HostFacts, ToolInfo, ToolKind
from . import paths
from .envcache import tool_env
from .session.tclshell import TOOL_SPECS

log = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"\bv?(\d{4}\.\d+(?:\.\d+)?)\b")


def exe_name(kind: str) -> str:
    spec = TOOL_SPECS.get(kind)
    if spec is None:
        raise ToolNotFound(
            f"unsupported tool kind {kind!r}; known: {sorted(TOOL_SPECS)}"
        )
    return spec.exe


async def resolve(settings_sh: str, kind: str) -> tuple[str, dict[str, str]]:
    """Return (absolute exe path, environment) for a tool. Raises ToolNotFound."""
    env = await tool_env(Path(settings_sh).expanduser())
    name = exe_name(kind)
    exe = shutil.which(name, path=env.get("PATH", ""))
    if exe is None:
        raise ToolNotFound(f"{name} not on PATH after sourcing {settings_sh}")
    return exe, env


async def probe(label: str, settings_sh: str, kind: str = ToolKind.VIVADO) -> ToolInfo:
    """Non-raising inspection of one configured tool, for ``host_status``."""
    path = Path(settings_sh).expanduser()
    if not path.is_file():
        return ToolInfo(
            label=label,
            settings_sh=str(path),
            kind=kind,
            exists=False,
            error="settings script not found",
        )
    try:
        exe, env = await resolve(str(path), kind)
    except ToolNotFound as exc:
        return ToolInfo(
            label=label, settings_sh=str(path), kind=kind, exists=False, error=str(exc)
        )
    return ToolInfo(
        label=label,
        settings_sh=str(path),
        kind=kind,
        exists=True,
        exe=exe,
        version=await _version(exe, env),
    )


async def _version(exe: str, env: dict[str, str]) -> str | None:
    """``vivado -version`` costs a second or two, so cache it per binary."""
    try:
        stat = os.stat(exe)
    except OSError:
        return None
    key = hashlib.sha256(f"{exe}:{stat.st_mtime_ns}".encode()).hexdigest()[:16]
    cache = paths.CACHE / f"ver-{key}.json"
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8")).get("version")
        except ValueError:
            cache.unlink(missing_ok=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            "-version",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 60.0)
    except (OSError, TimeoutError) as exc:
        log.warning("version probe for %s failed: %s", exe, exc)
        return None

    text = out.decode("utf-8", "replace")
    match = _VERSION_RE.search(text)
    version = match.group(1) if match else text.strip().splitlines()[0][:80] or None
    paths.CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"version": version}), encoding="utf-8")
    return version


def _meminfo_gb() -> tuple[float, float]:
    total = available = 0.0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            name, _, rest = line.partition(":")
            if name == "MemTotal":
                total = int(rest.split()[0]) / 1024 / 1024
            elif name == "MemAvailable":
                available = int(rest.split()[0]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        pass
    return round(total, 1), round(available, 1)


def host_facts() -> HostFacts:
    usage = shutil.disk_usage(paths.HOME if paths.HOME.exists() else Path.home())
    total_gb, avail_gb = _meminfo_gb()
    return HostFacts(
        hostname=socket.gethostname(),
        cores=os.cpu_count() or 1,
        load1=round(os.getloadavg()[0], 2),
        mem_total_gb=total_gb,
        mem_available_gb=avail_gb,
        disk_total_gb=round(usage.total / 1024**3, 1),
        disk_free_gb=round(usage.free / 1024**3, 1),
        home=str(paths.HOME),
    )
