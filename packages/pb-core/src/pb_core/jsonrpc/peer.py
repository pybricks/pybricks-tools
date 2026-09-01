# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""A symmetric JSON-RPC peer.

Both sides of the connection run the same object. The service does not send
requests to clients today, but making the peer symmetric means it will not need
redesigning when it does.

Structure follows the repository's async rules. :func:`connect_peer` is an async
context manager owning exactly two long-lived tasks, a reader and a writer, plus
one short-lived task per inbound request. When the block exits, every one of
them has finished, so no work outlives the connection.

Backpressure is deliberately asymmetric:

* **Responses** are sent with ``await``, so a slow peer slows the handler that
  produced them. That is harmless, because the handler has already finished its
  real work.
* **Notifications** are sent without blocking and are dropped if the queue is
  full, with a count kept. This matters upstream: hub output originates in a
  Bluetooth callback that must never block, so there must be no path by which a
  suspended client applies backpressure to the radio.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import anyio
from anyio.abc import ByteStream, TaskGroup, TaskStatus

from pb_core.jsonrpc.errors import ErrorCode, JsonRpcError, ProtocolError
from pb_core.jsonrpc.framing import LineFramer
from pb_core.jsonrpc.messages import (
    CANCEL_METHOD,
    Message,
    Notification,
    Request,
    RequestId,
    Response,
    error_response,
)

logger = logging.getLogger(__name__)

__all__ = ["JsonRpcPeer", "MethodHandler", "connect_peer"]

MethodHandler = Callable[[Request], Coroutine[Any, Any, Any]]
"""Handles one inbound request and returns its result.

Raise :class:`~pb_core.jsonrpc.errors.JsonRpcError` to return a specific error
code; any other exception becomes an internal error.
"""

OUTBOX_SIZE: Final = 2048


@dataclass
class _Pending:
    """A call awaiting its response."""

    done: anyio.Event = field(default_factory=anyio.Event)
    result: Any = None
    error: BaseException | None = None


class PeerClosedError(Exception):
    """The connection ended before the call could be answered."""


class JsonRpcPeer:
    """One end of a JSON-RPC connection.

    Do not construct directly; use :func:`connect_peer`, which owns the tasks
    this needs to run.
    """

    def __init__(
        self,
        stream: ByteStream,
        task_group: TaskGroup,
        handler: MethodHandler | None = None,
    ) -> None:
        """Set up a peer.

        Args:
            stream: The transport.
            task_group: Group owning this peer's tasks.
            handler: Called for inbound requests. Omit for a client that only
                makes calls and receives notifications.
        """
        self._framer = LineFramer(stream)
        self._tg = task_group
        self._handler = handler
        self._ids = itertools.count(1)
        self._pending: dict[RequestId, _Pending] = {}
        self._inflight: dict[RequestId, anyio.CancelScope] = {}
        self._outbox_tx, self._outbox_rx = anyio.create_memory_object_stream[Message](
            OUTBOX_SIZE
        )
        self._closed = anyio.Event()
        self._drained = anyio.Event()
        """Set once the writer has stopped, whether drained or cancelled."""

        self.dropped_notifications = 0
        """Notifications discarded because this peer was not keeping up."""

    # ------------------------------------------------------------ outbound

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request and wait for its response.

        There is deliberately no timeout argument. Wrap the call in
        :func:`anyio.fail_after` or :func:`anyio.move_on_after` instead;
        cancelling the wait also tells the peer to stop working on it.

        Args:
            method: Method name.
            params: Named parameters.

        Returns:
            The ``result`` member of the response.

        Raises:
            JsonRpcError: If the peer returned an error.
            PeerClosedError: If the connection ended first.
        """
        request_id = next(self._ids)
        pending = _Pending()
        self._pending[request_id] = pending

        try:
            await self._outbox_tx.send(Request(request_id, method, params))

            try:
                await pending.done.wait()
            except anyio.get_cancelled_exc_class():
                # Our deadline expiring only abandons this side's wait, so the
                # peer must be told too or it keeps working. No shield needed:
                # this is a synchronous, non-blocking send.
                self._notify_nowait(Notification(CANCEL_METHOD, {"id": request_id}))
                raise
        finally:
            self._pending.pop(request_id, None)

        if pending.error is not None:
            raise pending.error

        return pending.result

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification, dropping it if this peer is not keeping up.

        Synchronous and non-blocking by design: callers include code that must
        never block, and a notification is by definition something the protocol
        can lose.

        Args:
            method: Method name.
            params: Named parameters.
        """
        self._notify_nowait(Notification(method, params))

    def _notify_nowait(self, notification: Notification) -> None:
        try:
            self._outbox_tx.send_nowait(notification)
        except anyio.WouldBlock:
            self.dropped_notifications += 1
            if self.dropped_notifications in (1, 10, 100) or (
                self.dropped_notifications % 1000 == 0
            ):
                logger.warning(
                    "peer is not keeping up; dropped %d notifications so far",
                    self.dropped_notifications,
                )
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            logger.debug("dropping %s: connection closed", notification.method)

    # ------------------------------------------------------------- running

    async def run(self, *, task_status: TaskStatus[None]) -> None:
        """Run the reader and writer until the connection ends.

        Args:
            task_status: Signalled once both tasks are running.
        """

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._writer_loop)
                task_status.started()
                await self._reader_loop()
                # Reader returned, so the peer is gone: stop the writer too.
                tg.cancel_scope.cancel()
        finally:
            # Both ends, or the receive half warns when it is collected.
            self._outbox_tx.close()
            self._outbox_rx.close()

    async def wait_closed(self) -> None:
        """Wait until the peer disconnects."""
        await self._closed.wait()

    async def drain(self) -> None:
        """Send everything already queued, and refuse anything more.

        For the one message that must not be lost to a teardown: the reply to
        the request that asked for the teardown. Cancelling the writer instead
        drops it, and the caller is left believing its request went unanswered
        when in fact it was obeyed.

        Waits for the writer to finish, so callers should bound it -- a peer
        that has stopped reading must not be able to hold this open.
        """
        self._outbox_tx.close()
        await self._drained.wait()

    async def _writer_loop(self) -> None:
        try:
            async for message in self._outbox_rx:
                await self._framer.send(message)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            logger.debug("writer stopping: connection gone")
        finally:
            # Set however the writer ends, including cancellation, so that
            # nothing waiting on a drain can hang.
            self._drained.set()

    async def _reader_loop(self) -> None:
        try:
            while True:
                try:
                    message = await self._framer.receive()
                except ProtocolError as exc:
                    logger.warning("protocol error, closing connection: %s", exc)
                    await self._try_send(
                        error_response(None, ErrorCode.PARSE_ERROR, str(exc))
                    )
                    return

                self._dispatch(message)
        except (
            anyio.EndOfStream,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ):
            logger.debug("connection closed by peer")
        finally:
            self._closed.set()
            self._outbox_tx.close()
            self._fail_pending()

    def _fail_pending(self) -> None:
        """Wake every outstanding call, so none of them hang forever."""

        for request_id, pending in list(self._pending.items()):
            pending.error = PeerClosedError(
                "connection closed before the response arrived"
            )
            pending.done.set()
            logger.debug("failed pending request %s: connection closed", request_id)

    # ---------------------------------------------------------- dispatching

    def _dispatch(self, message: Message) -> None:
        if isinstance(message, Response):
            self._resolve(message)
        elif isinstance(message, Notification):
            if message.method == CANCEL_METHOD:
                self._cancel_inflight(message)
            elif self._handler is not None:
                self._tg.start_soon(self._run_notification, message)
        else:
            self._tg.start_soon(self._run_request, message)

    def _resolve(self, response: Response) -> None:
        if response.id is None:
            logger.warning("peer reported: %s", response.error)
            return

        pending = self._pending.get(response.id)
        if pending is None:
            logger.debug("response for unknown request %s", response.id)
            return

        if response.error is not None:
            pending.error = JsonRpcError(
                response.error.code, response.error.message, response.error.data
            )
        else:
            pending.result = response.result

        pending.done.set()

    def _cancel_inflight(self, notification: Notification) -> None:
        params = notification.params or {}
        request_id = params.get("id")
        if not isinstance(request_id, int | str):
            return

        scope = self._inflight.get(request_id)
        if scope is not None:
            logger.debug("cancelling in-flight request %s", request_id)
            scope.cancel()

    async def _run_notification(self, notification: Notification) -> None:
        assert self._handler is not None

        try:
            await self._handler(Request(0, notification.method, notification.params))
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:
            # Nothing to reply to, so a failed notification handler is only
            # ever a log line.
            logger.exception("notification handler failed: %s", notification.method)

    async def _run_request(self, request: Request) -> None:
        if self._handler is None:
            await self._try_send(
                error_response(
                    request.id,
                    ErrorCode.METHOD_NOT_FOUND,
                    "this peer serves no methods",
                )
            )
            return

        # Pre-set so that a scope cancelled by $/cancelRequest reports exactly
        # that. A cancellation from the enclosing task group instead propagates
        # out of the scope, and nothing is sent.
        response = error_response(
            request.id, ErrorCode.REQUEST_CANCELLED, "request cancelled"
        )

        with anyio.CancelScope() as scope:
            self._inflight[request.id] = scope

            try:
                result = await self._handler(request)
                response = Response(request.id, result=result)
            except JsonRpcError as exc:
                response = error_response(request.id, exc.code, exc.message, exc.data)
            except Exception as exc:
                logger.exception("handler failed: %s", request.method)
                response = error_response(
                    request.id, ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
                )
            finally:
                self._inflight.pop(request.id, None)

        await self._try_send(response)

    async def _try_send(self, message: Message) -> None:
        try:
            await self._outbox_tx.send(message)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            logger.debug("dropping outbound message: connection closed")


@asynccontextmanager
async def connect_peer(
    stream: ByteStream, handler: MethodHandler | None = None
) -> AsyncGenerator[JsonRpcPeer, None]:
    """Run a JSON-RPC peer for the duration of the block.

    Args:
        stream: The transport, already connected.
        handler: Called for inbound requests. Omit for a pure client.

    Yields:
        The peer, ready to use.
    """

    async with anyio.create_task_group() as tg:
        peer = JsonRpcPeer(stream, tg, handler)
        await tg.start(peer.run)

        try:
            yield peer
        finally:
            # Everything this peer started is inside tg, so cancelling it here
            # is what makes the black-box rule hold for the whole connection.
            tg.cancel_scope.cancel()
