"""Bounded read-only file access inside a configured workspace."""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from ..common.errors import BadRequest, NotFound
from .workspace import PathMapper

_MAX_READ = 64 * 1024
_MAX_GREP_FILES = 5000


def file_read(
    mapper: PathMapper, relative: str, offset: int = 0, limit: int = 12000
) -> dict[str, Any]:
    path = mapper.to_remote(relative)
    if not path.is_file():
        raise NotFound(f"not a file in the workspace: {relative!r}")
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), _MAX_READ))
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(limit + 1)
    truncated = len(data) > limit
    data = data[:limit]
    return {
        "path": relative,
        "offset": offset,
        "next_offset": offset + len(data),
        "text": data.decode("utf-8", "replace"),
        "truncated": truncated,
        "size": path.stat().st_size,
    }


def file_grep(
    mapper: PathMapper,
    pattern: str,
    glob: str = "**/*",
    limit: int = 200,
) -> dict[str, Any]:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise BadRequest(f"invalid grep regex: {exc}") from exc
    limit = max(1, min(int(limit), 1000))
    matches: list[dict[str, Any]] = []
    scanned = 0
    roots = ((mapper.work, ""), (mapper.build_path, mapper.build + "/"))
    for root, prefix in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if scanned >= _MAX_GREP_FILES or len(matches) >= limit:
                break
            if not path.is_file() or path.is_symlink():
                continue
            relative = prefix + path.relative_to(root).as_posix()
            if not fnmatch.fnmatch(relative, glob):
                continue
            scanned += 1
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for number, line in enumerate(fh, 1):
                        if regex.search(line):
                            matches.append(
                                {
                                    "path": relative,
                                    "line": number,
                                    "text": line.rstrip("\n")[-2000:],
                                }
                            )
                            if len(matches) >= limit:
                                break
            except OSError:
                continue
    return {
        "matches": matches,
        "count": len(matches),
        "scanned_files": scanned,
        "truncated": len(matches) >= limit or scanned >= _MAX_GREP_FILES,
    }
