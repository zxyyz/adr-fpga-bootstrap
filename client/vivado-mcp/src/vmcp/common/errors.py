"""Typed errors shared by client and agent.

``code`` is what crosses the wire; the client re-raises a :class:`RemoteError`
carrying the original code so callers can branch on it without string matching.
"""


class VmcpError(Exception):
    code = "internal"


class ProtocolError(VmcpError):
    code = "protocol"


class TransportError(VmcpError):
    """The link to the agent is gone. Retrying may succeed."""

    code = "transport"


class NotFound(VmcpError):
    code = "not_found"


class BadRequest(VmcpError):
    code = "bad_request"


class SessionError(VmcpError):
    code = "session"


class SessionBusy(SessionError):
    code = "session_busy"


class SessionDead(SessionError):
    code = "session_dead"


class EvalTimeout(SessionError):
    """The Tcl interpreter did not return within the deadline.

    The command may still be running; the session stays unusable until its
    sentinel arrives.
    """

    code = "eval_timeout"


class ToolNotFound(VmcpError):
    code = "tool_not_found"


class RemoteError(VmcpError):
    """An error raised by the peer, reconstructed locally."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"
