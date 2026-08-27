"""Cached ``settings64.sh`` environment snapshots.

Sourcing a Xilinx settings script costs a shell startup plus a few hundred
milliseconds of environment munging.  We pay it once per (script, mtime) and
then ``execve`` tools directly with the recorded environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
from pathlib import Path

from ..common.errors import ToolNotFound
from . import paths

log = logging.getLogger(__name__)

#: Variables that differ between the snapshot shell and the tool we launch, and
#: would otherwise pin a tool to a stale directory or a dead ssh connection.
_VOLATILE = frozenset(
    {
        "_",
        "SHLVL",
        "PWD",
        "OLDPWD",
        "SSH_CLIENT",
        "SSH_CONNECTION",
        "SSH_TTY",
        "SSH_AUTH_SOCK",
        "TERM",
        "COLUMNS",
        "LINES",
    }
)


def _cache_path(settings_sh: Path) -> Path:
    stat = settings_sh.stat()
    key = hashlib.sha256(
        f"{settings_sh}:{stat.st_mtime_ns}:{stat.st_size}".encode()
    ).hexdigest()[:16]
    return paths.CACHE / f"env-{key}.json"


async def tool_env(settings_sh: Path) -> dict[str, str]:
    if not settings_sh.is_file():
        raise ToolNotFound(f"settings script not found: {settings_sh}")

    cache = _cache_path(settings_sh)
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            cache.unlink(missing_ok=True)

    cmd = f"source {shlex.quote(str(settings_sh))} >/dev/null 2>&1 && env -0"
    proc = await asyncio.create_subprocess_exec(
        "bash",
        "-lc",
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise ToolNotFound(
            f"sourcing {settings_sh} failed (rc={proc.returncode}): "
            f"{err.decode(errors='replace').strip()[:400]}"
        )

    env: dict[str, str] = {}
    for chunk in out.decode("utf-8", "replace").split("\0"):
        name, sep, value = chunk.partition("=")
        if not sep or name in _VOLATILE or name.startswith("BASH_FUNC_"):
            continue
        env[name] = value
    if "PATH" not in env:
        raise ToolNotFound(f"{settings_sh} produced no PATH")

    paths.CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(env, indent=0), encoding="utf-8")
    log.info("cached environment for %s (%d vars)", settings_sh, len(env))
    return env
