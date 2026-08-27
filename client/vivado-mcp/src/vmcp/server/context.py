"""Shared state for the MCP tool implementations."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config, load
from .transport.ssh import SshLink


@dataclass(slots=True)
class Ctx:
    cfg: Config
    link: SshLink

    @classmethod
    def create(cls) -> Ctx:
        cfg = load()
        return cls(cfg=cfg, link=SshLink(cfg.host))


#: Responses go straight into a model's context window, so every tool clamps its
#: text fields. Full output stays on the build server and is fetched on demand.
MAX_TEXT = 12000


def clamp(text: str, limit: int = MAX_TEXT) -> tuple[str, bool]:
    """Return (possibly truncated text, whether it was truncated).

    Keeps the tail: for tool logs the last lines carry the error.
    """
    if len(text) <= limit:
        return text, False
    kept = text[-limit:]
    newline = kept.find("\n")
    if 0 <= newline < 200:
        kept = kept[newline + 1 :]
    return f"...[{len(text) - len(kept)} chars omitted]...\n{kept}", True
