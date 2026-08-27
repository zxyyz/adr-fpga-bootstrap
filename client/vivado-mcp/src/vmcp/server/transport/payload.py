"""Build the agent zipapp.

The archive is built with a fixed timestamp on every member so identical sources
produce identical bytes.  Its sha256 is the agent's identity, and a stable
identity is what lets the version handshake say "already up to date" instead of
redeploying on every call.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import zipapp
from pathlib import Path

import vmcp

#: 1980-01-01, the earliest timestamp the zip format can represent.
_FIXED_MTIME = 315532800

#: Only these subpackages go to the build server. ``vmcp.server`` and
#: ``vmcp.cli`` stay local because they may use third-party dependencies.
_AGENT_PARTS = ("__init__.py", "common", "agent")

CACHE_DIR = Path(os.environ.get("VMCP_CACHE", "~/.cache/vivado-mcp")).expanduser()


def _iter_sources(root: Path) -> list[Path]:
    files: list[Path] = []
    for part in _AGENT_PARTS:
        target = root / part
        if target.is_file():
            files.append(target)
            continue
        files.extend(
            p for p in sorted(target.rglob("*.py")) if "__pycache__" not in p.parts
        )
    return files


def source_hash() -> str:
    root = Path(vmcp.__file__).parent
    digest = hashlib.sha256()
    for path in _iter_sources(root):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build(cache_dir: Path | None = None) -> tuple[Path, str]:
    """Return (path to the zipapp, its sha256). Cached on source hash."""
    cache_dir = cache_dir or CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    src_hash = source_hash()
    target = cache_dir / f"vmcp-agent-{src_hash[:16]}.pyz"
    sha_file = target.with_suffix(".pyz.sha256")
    if target.is_file() and sha_file.is_file():
        return target, sha_file.read_text(encoding="utf-8").strip()

    root = Path(vmcp.__file__).parent
    with tempfile.TemporaryDirectory(prefix="vmcp-payload-") as tmp:
        staging = Path(tmp) / "vmcp"
        for path in _iter_sources(root):
            dest = staging / path.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        for path in sorted(staging.rglob("*")):
            os.utime(path, (_FIXED_MTIME, _FIXED_MTIME))
        os.utime(staging, (_FIXED_MTIME, _FIXED_MTIME))
        zipapp.create_archive(
            staging.parent,
            target=target,
            interpreter="/usr/bin/env python3",
            main="vmcp.agent.main:main",
            compressed=True,
        )
    target.chmod(0o755)
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    sha_file.write_text(sha, encoding="utf-8")
    return target, sha
