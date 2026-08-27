"""Opaque, collision-resistant identifiers for sessions and jobs."""

from __future__ import annotations

import secrets


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"
