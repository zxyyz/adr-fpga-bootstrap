"""Symmetric JSON-RPC-ish peer over a pair of asyncio streams.

Both the daemon (one peer per unix-socket connection) and the MCP server (one
peer per ssh bridge) use this class.  Requests are dispatched as independent
tasks so a long-poll handler never blocks unrelated traffic on the same link —
this is required by long-running calls such as ``job_wait``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .errors import RemoteError, TransportError, VmcpError
from .framing import read_frame, write_frame

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[Any] | Any]


class Peer:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        handlers: dict[str, Handler] | None = None,
        name: str = "peer",
    ) -> None:
        self.name = name
        self._reader = reader
        self._writer = writer
        self._handlers = handlers or {}
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._wlock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._incoming: dict[str, asyncio.Task[Any]] = {}
        self._done = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._done.is_set()

    async def serve(self) -> None:
        """Read frames until EOF. Always terminates by failing pending calls."""
        reason: BaseException = TransportError("connection closed")
        try:
            while True:
                self._dispatch(await read_frame(self._reader))
        except asyncio.IncompleteReadError:
            pass
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            reason = TransportError(f"link failed: {exc}")
        # A malformed frame must tear the link down, not leave callers waiting
        # forever on futures nothing will ever resolve.
        except Exception as exc:  # pylint: disable=broad-exception-caught
            reason = exc
            log.exception("%s: fatal error in read loop", self.name)
        finally:
            await self._teardown(reason)

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        if self.closed:
            raise TransportError("peer is closed")
        rid = str(next(self._ids))
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"id": rid, "method": method, "params": params or {}})
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.CancelledError, TimeoutError):
            with contextlib.suppress(TransportError):
                await self.notify("$/cancel", {"id": rid})
            raise
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def aclose(self) -> None:
        await self._teardown(TransportError("closed locally"))

    # -- internals ---------------------------------------------------------

    def _dispatch(self, frame: Any) -> None:
        if not isinstance(frame, dict):
            raise TransportError(f"expected object frame, got {type(frame).__name__}")
        if "method" in frame:
            if frame["method"] == "$/cancel":
                task = self._incoming.get(str((frame.get("params") or {}).get("id")))
                if task is not None:
                    task.cancel()
                return
            task = self._spawn(self._serve_request(frame))
            if frame.get("id") is not None:
                rid = str(frame["id"])
                self._incoming[rid] = task
                task.add_done_callback(
                    lambda _task, key=rid: self._incoming.pop(key, None)
                )
            return
        fut = self._pending.pop(str(frame.get("id")), None)
        if fut is None or fut.done():
            return  # response to a call we already gave up on
        err = frame.get("error")
        if err:
            fut.set_exception(
                RemoteError(err.get("code", "internal"), err.get("message", ""))
            )
        else:
            fut.set_result(frame.get("result"))

    async def _serve_request(self, frame: dict[str, Any]) -> None:
        method, rid = frame["method"], frame.get("id")
        try:
            handler = self._handlers.get(method)
            if handler is None:
                raise RemoteError("no_such_method", f"unknown method {method!r}")
            result = handler(frame.get("params") or {})
            if isinstance(result, Awaitable):
                result = await result
            reply: dict[str, Any] = {"id": rid, "result": result}
        except VmcpError as exc:
            reply = {"id": rid, "error": {"code": exc.code, "message": str(exc)}}
        # One failing handler must not take down the link. CancelledError derives
        # from BaseException, so cancellation still propagates.
        except Exception as exc:  # pylint: disable=broad-exception-caught
            log.exception("%s: handler %s failed", self.name, method)
            reply = {
                "id": rid,
                "error": {
                    "code": "internal",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }
        if rid is not None:
            try:
                await self._send(reply)
            except TransportError:
                pass  # link died while replying; nothing left to tell

    async def _send(self, frame: dict[str, Any]) -> None:
        async with self._wlock:
            try:
                await write_frame(self._writer, frame)
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                raise TransportError(f"write failed: {exc}") from exc

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _teardown(self, reason: BaseException) -> None:
        if self._done.is_set():
            return
        self._done.set()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(reason)
        self._pending.clear()
        for task in list(self._tasks):
            task.cancel()
        self._incoming.clear()
        try:
            self._writer.close()
        except OSError:
            pass
