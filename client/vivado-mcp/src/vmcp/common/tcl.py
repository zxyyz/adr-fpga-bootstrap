"""Shared helpers for safely embedding UTF-8 values in generated Tcl."""

from __future__ import annotations

import base64

TCL_DECODE_PROC = (
    "proc vmcp_decode {value} {",
    "    return [encoding convertfrom utf-8 [binary decode base64 $value]]",
    "}",
)


def b64encode_utf8(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")
