"""Durable git workspaces and the single workspace-relative path mapper."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..common.errors import BadRequest, NotFound
from ..common.jsonio import read_json, write_json
from . import paths

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_COMMIT = re.compile(r"[0-9a-fA-F]{40,64}\Z")


def _relative(value: str, *, allow_root: bool = False) -> PurePosixPath:
    if not isinstance(value, str) or "\0" in value or "\\" in value:
        raise BadRequest("workspace paths must be POSIX relative paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        if allow_root and value in {"", "."}:
            return PurePosixPath(".")
        raise BadRequest(f"path must stay inside the workspace: {value!r}")
    return path


class PathMapper:
    """Map the virtual workspace tree to ``work/`` plus the sibling build tree."""

    def __init__(self, root: Path, build: str = "build") -> None:
        self.root = root.resolve()
        self.work = (root / "work").resolve()
        self.build_path = (root / "build").resolve()
        self.build_parts = _relative(build).parts
        self.build = "/".join(self.build_parts)

    def to_remote(self, relative: str, *, allow_root: bool = False) -> Path:
        logical = _relative(relative, allow_root=allow_root)
        parts = logical.parts
        if parts[: len(self.build_parts)] == self.build_parts:
            suffix = parts[len(self.build_parts) :]
            candidate = self.build_path.joinpath(*suffix)
            base = self.build_path
        else:
            candidate = self.work.joinpath(*parts)
            base = self.work
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(base):
            raise BadRequest(f"path escapes workspace through a symlink: {relative!r}")
        return resolved

    def to_relative(self, remote: Path) -> str:
        resolved = remote.resolve(strict=False)
        if resolved.is_relative_to(self.build_path):
            suffix = resolved.relative_to(self.build_path)
            return str(PurePosixPath(*self.build_parts, *suffix.parts))
        if resolved.is_relative_to(self.work):
            return resolved.relative_to(self.work).as_posix()
        raise BadRequest(f"remote path is outside workspace: {remote}")


class WorkspaceManager:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def mapper(self, name: str, build: str = "build") -> PathMapper:
        if not _NAME.fullmatch(name):
            raise BadRequest(f"invalid workspace name: {name!r}")
        return PathMapper(paths.WORKSPACES / name, build)

    async def prepare(self, name: str, build: str = "build") -> dict[str, Any]:
        mapper = self.mapper(name, build)
        async with self._locks.setdefault(name, asyncio.Lock()):
            mapper.root.mkdir(parents=True, exist_ok=True)
            mapper.build_path.mkdir(parents=True, exist_ok=True)
            repo = mapper.root / "repo.git"
            if not repo.is_dir():
                await self._git("init", "--bare", str(repo))
            state = mapper.root / "state.json"
            if not state.exists():
                write_json(
                    state,
                    {
                        "name": name,
                        "build": mapper.build,
                        "commit": None,
                        "updated_at": None,
                    },
                )
        return self.status(name, build)

    async def activate(
        self, name: str, commit: str, build: str = "build"
    ) -> dict[str, Any]:
        if not _COMMIT.fullmatch(commit):
            raise BadRequest(f"invalid git commit: {commit!r}")
        mapper = self.mapper(name, build)
        async with self._locks.setdefault(name, asyncio.Lock()):
            repo = mapper.root / "repo.git"
            if not repo.is_dir():
                raise NotFound(f"workspace {name!r} has not been prepared")
            await self._git(
                "--git-dir", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"
            )
            if not (mapper.work / ".git").is_dir():
                if mapper.work.exists() and any(mapper.work.iterdir()):
                    raise BadRequest(
                        f"workspace worktree is not a git checkout: {mapper.work}"
                    )
                mapper.work.parent.mkdir(parents=True, exist_ok=True)
                await self._git("clone", str(repo), str(mapper.work))
            await self._git(
                "-C",
                str(mapper.work),
                "fetch",
                "--force",
                "origin",
                "refs/vmcp/staging:refs/remotes/origin/vmcp-staging",
            )
            await self._git("-C", str(mapper.work), "reset", "--hard", commit)
            await self._git("-C", str(mapper.work), "clean", "-xfd", "--", ".")
            write_json(
                mapper.root / "state.json",
                {
                    "name": name,
                    "build": mapper.build,
                    "commit": commit.lower(),
                    "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
        return self.status(name, build)

    def status(self, name: str, build: str = "build") -> dict[str, Any]:
        mapper = self.mapper(name, build)
        repo = mapper.root / "repo.git"
        state_path = mapper.root / "state.json"
        state = read_json(state_path) if state_path.is_file() else {}
        return {
            "name": name,
            "exists": repo.is_dir(),
            "commit": state.get("commit"),
            "updated_at": state.get("updated_at"),
            "repo": str(repo),
            "work": str(mapper.work),
            "build": mapper.build,
            "build_path": str(mapper.build_path),
        }

    def resolve(
        self,
        name: str,
        relative: str,
        build: str = "build",
        *,
        must_exist: bool = False,
    ) -> dict[str, Any]:
        mapper = self.mapper(name, build)
        remote = mapper.to_remote(relative)
        if must_exist and not remote.exists():
            raise NotFound(f"workspace path does not exist: {relative!r}")
        return {
            "workspace": name,
            "path": relative,
            "remote": str(remote),
            "exists": remote.exists(),
            "is_file": remote.is_file(),
            "is_dir": remote.is_dir(),
        }

    @staticmethod
    async def _git(*args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "LC_ALL": "C"},
            )
        except OSError as exc:
            raise BadRequest(f"cannot run git on build host: {exc}") from exc
        out, err = await proc.communicate()
        if proc.returncode:
            detail = err.decode("utf-8", "replace").strip()[-2000:]
            raise BadRequest(f"git {' '.join(args[:3])} failed: {detail}")
        return out.decode("utf-8", "replace").strip()
