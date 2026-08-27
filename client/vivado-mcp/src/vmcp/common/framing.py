"""Length-prefixed JSON frames.

Used for both hops of the link: MCP server <-> ssh <-> ``vmcp-agent attach``
<-> unix socket <-> daemon.  ``attach`` copies bytes verbatim and never needs to
parse a frame, so this codec only has to agree between the two endpoints.
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

from .errors import ProtocolError

_HDR = struct.Struct("!I")
MAX_FRAME = 64 * 1024 * 1024


def encode_frame(obj: Any) -> bytes:
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ProtocolError(f"frame too large to send: {len(body)} bytes")
    return _HDR.pack(len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> Any:
    """Read one frame. Raises ``asyncio.IncompleteReadError`` at clean EOF."""
    (size,) = _HDR.unpack(await reader.readexactly(_HDR.size))
    if not 0 < size <= MAX_FRAME:
        raise ProtocolError(f"frame size {size} out of range")
    return json.loads(await reader.readexactly(size))


async def write_frame(writer: asyncio.StreamWriter, obj: Any) -> None:
    writer.write(encode_frame(obj))
    await writer.drain()
