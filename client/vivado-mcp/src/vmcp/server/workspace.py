"""Local half of git workspace synchronization."""

from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from ..common.errors import BadRequest
from .config import HostCfg, WorkspaceCfg


def validate_relative(value: str) -> str:
    if not isinstance(value, str) or "\0" in value or "\\" in value:
        raise BadRequest("workspace paths must be POSIX relative paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BadRequest(f"path must stay inside the workspace: {value!r}")
    return path.as_posix()


class GitWorkspace:
    def __init__(self, cfg: WorkspaceCfg, host: HostCfg) -> None:
        self.cfg = cfg
        self.host = host
        self.root = cfg.local

    async def status(self) -> dict[str, Any]:
        await self._check_repo()
        head = await self._git("rev-parse", "--verify", "HEAD", check=False)
        porcelain = await self._git("status", "--porcelain=v1", "-z")
        changed = self._parse_status(porcelain)
        return {
            "root": str(self.root),
            "head": head.strip() or None,
            "dirty": bool(changed),
            "changed": changed[:500],
            "truncated": len(changed) > 500,
        }

    async def staging_commit(self, selected: list[str] | None = None) -> dict[str, Any]:
        await self._check_repo()
        selected = [validate_relative(item) for item in selected] if selected else []
        fd, raw_index = tempfile.mkstemp(prefix="vmcp-index-")
        os.close(fd)
        Path(raw_index).unlink(missing_ok=True)
        env = {
            **os.environ,
            "GIT_INDEX_FILE": raw_index,
            "GIT_AUTHOR_NAME": "vivado-mcp",
            "GIT_AUTHOR_EMAIL": "vivado-mcp@localhost",
            "GIT_COMMITTER_NAME": "vivado-mcp",
            "GIT_COMMITTER_EMAIL": "vivado-mcp@localhost",
        }
        try:
            head = (
                await self._git("rev-parse", "--verify", "HEAD", check=False)
            ).strip()
            if head:
                await self._git("read-tree", "HEAD", env=env)
            else:
                await self._git("read-tree", "--empty", env=env)
            add_args = ["add", "-A", "--"]
            add_args.extend(selected or ["."])
            await self._git(*add_args, env=env)
            tree = (await self._git("write-tree", env=env)).strip()
            commit_args = ["commit-tree", tree]
            if head:
                commit_args += ["-p", head]
            commit_args += ["-m", "vivado-mcp staging"]
            commit = (await self._git(*commit_args, env=env)).strip()
        finally:
            Path(raw_index).unlink(missing_ok=True)
        return {"commit": commit, "tree": tree, "parent": head or None}

    async def push(self, commit: str, remote_repo: str) -> None:
        remote = f"{self.host.ssh}:{remote_repo}"
        ssh = ["ssh"]
        for option in self.host.ssh_options:
            ssh += ["-o", option]
        env = {**os.environ, "GIT_SSH_COMMAND": shlex.join(ssh), "LC_ALL": "C"}
        await self._git(
            "push",
            "--force",
            remote,
            f"{commit}:refs/vmcp/staging",
            env=env,
        )

    async def _check_repo(self) -> None:
        if not self.root.is_dir():
            raise BadRequest(f"workspace local directory does not exist: {self.root}")
        top = (await self._git("rev-parse", "--show-toplevel")).strip()
        if Path(top).resolve() != self.root.resolve():
            raise BadRequest(
                f"workspace.local must be the git root ({top}), not {self.root}"
            )

    async def _git(
        self, *args: str, env: dict[str, str] | None = None, check: bool = True
    ) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(self.root),
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env or {**os.environ, "LC_ALL": "C"},
            )
        except OSError as exc:
            raise BadRequest(f"cannot run local git: {exc}") from exc
        out, err = await proc.communicate()
        if check and proc.returncode:
            detail = err.decode("utf-8", "replace").strip()[-2000:]
            raise BadRequest(f"git {' '.join(args[:3])} failed: {detail}")
        return out.decode("utf-8", "replace")

    @staticmethod
    def _parse_status(raw: str) -> list[str]:
        entries = raw.split("\0")
        changed: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            path = entry[3:] if len(entry) >= 4 else entry
            if entry[:2] in {"R ", " R", "C ", " C"} and index < len(entries):
                path = entries[index]
                index += 1
            changed.append(path)
        return changed


async def rsync_pull(
    host: HostCfg, remote: str, local: Path, *, is_dir: bool = False
) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    ssh = ["ssh"]
    for option in host.ssh_options:
        ssh += ["-o", option]
    source = f"{host.ssh}:{remote}{'/' if is_dir else ''}"
    argv = ["rsync", "-a", "--protect-args", "-e", shlex.join(ssh), source, str(local)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise BadRequest(f"cannot run rsync: {exc}") from exc
    _out, err = await proc.communicate()
    if proc.returncode:
        raise BadRequest(
            "rsync pull failed: " + err.decode("utf-8", "replace").strip()[-2000:]
        )
