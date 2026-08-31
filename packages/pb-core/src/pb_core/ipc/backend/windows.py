# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Windows named pipe transport.

This is the one place in the project that uses the asyncio transport/protocol
callback API, which the rest of the codebase deliberately avoids. anyio has no
named pipe support, and asyncio cannot serve AF_UNIX sockets on the proactor
loop that this backend requires, so there is no alternative. The bridge is
confined to this module and exposes only :class:`anyio.abc.ByteStream`.

Why this wraps :class:`asyncio.StreamReader` rather than draining
``data_received`` into a memory object stream: ``StreamReaderProtocol`` already
implements correct high and low watermark flow control against
``transport.pause_reading()``, so the peer gets real backpressure.
Reimplementing that by hand means either an unbounded buffer or a hand-rolled
watermark scheme.

Because it wraps ``StreamReader`` and ``StreamWriter``, at most one task may
call :meth:`receive` and one :meth:`send` on a given stream at a time.
Concurrent readers are undefined in asyncio. The session design upholds this by
giving every connection exactly one reader task and one writer task.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Any, final

import anyio
from anyio.abc import ByteStream, TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from typing_extensions import override

from pb_core.ipc.address import Endpoint
from pb_core.ipc.backend.provider import IpcBackend, IpcListener, StreamHandler, owned
from pb_core.ipc.errors import AddressInUseError

logger = logging.getLogger(__name__)

__all__ = ["WindowsPipeBackend", "pipe_path"]

_READ_LIMIT = 1024 * 1024
_ACCEPT_BACKLOG = 64
_CLOSE_TIMEOUT = 2.0
_PIPE_PREFIX = "\\\\.\\pipe\\"


def pipe_path(endpoint: Endpoint) -> str:
    r"""Return the full pipe path for a pipe endpoint.

    Args:
        endpoint: A ``pipe://`` endpoint, whose target may be either a bare
            name or an already-qualified path.

    Returns:
        The qualified path, for example ``\\.\pipe\pybricks-pb-service-david``.
    """
    target = endpoint.target
    if target.startswith("\\\\"):
        return target
    return _PIPE_PREFIX + target


@final
class _PipeStream(ByteStream):
    """An anyio byte stream over an asyncio pipe connection."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer

    @override
    async def receive(self, max_bytes: int = 65536) -> bytes:
        try:
            data = await self._reader.read(max_bytes)
        except OSError as exc:
            raise anyio.BrokenResourceError from exc
        if not data:
            raise anyio.EndOfStream
        return data

    @override
    async def send(self, item: bytes) -> None:
        try:
            self._writer.write(item)
            await self._writer.drain()
        except OSError as exc:
            raise anyio.BrokenResourceError from exc

    @override
    async def send_eof(self) -> None:
        # _ProactorDuplexPipeTransport.can_write_eof() is False, so a named pipe
        # cannot be half-closed. Closing the connection is the only EOF signal.
        raise NotImplementedError("named pipes cannot be half-closed")

    @override
    async def aclose(self) -> None:
        self._writer.close()
        # Shielded so teardown still completes when we are being cancelled, and
        # bounded so that a wedged peer cannot hang shutdown.
        with (
            anyio.CancelScope(shield=True),
            anyio.move_on_after(_CLOSE_TIMEOUT),
            suppress(OSError),
        ):
            await self._writer.wait_closed()


@final
class _PipeListener(IpcListener):
    """Accepts named pipe connections into a bounded queue."""

    def __init__(
        self,
        servers: list[Any],
        tx: MemoryObjectSendStream[ByteStream],
        rx: MemoryObjectReceiveStream[ByteStream],
    ) -> None:
        self._servers = servers
        self._tx = tx
        self._rx = rx

    @override
    async def serve(self, handler: StreamHandler, task_group: TaskGroup) -> None:
        wrapped = owned(handler)
        async for stream in self._rx:
            task_group.start_soon(wrapped, stream)

    @override
    async def aclose(self) -> None:
        for server in self._servers:
            close = getattr(server, "close", None)
            if callable(close):
                close()
        self._tx.close()
        # Close anything accepted but never served, or it leaks on shutdown.
        with suppress(anyio.WouldBlock, anyio.EndOfStream, anyio.ClosedResourceError):
            while True:
                await self._rx.receive_nowait().aclose()
        self._rx.close()


@final
class WindowsPipeBackend(IpcBackend):
    """Named pipes served through the asyncio proactor event loop."""

    @override
    def connect(self, endpoint: Endpoint) -> AbstractAsyncContextManager[ByteStream]:
        return self._connect(endpoint)

    @asynccontextmanager
    async def _connect(self, endpoint: Endpoint) -> AsyncGenerator[ByteStream, None]:
        loop: Any = asyncio.get_running_loop()
        path = pipe_path(endpoint)
        reader = asyncio.StreamReader(limit=_READ_LIMIT)
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            transport, _ = await loop.create_pipe_connection(lambda: protocol, path)
        except FileNotFoundError:
            # The pipe does not exist, so no service is running. Note that
            # connect_pipe retries ERROR_PIPE_BUSY internally but raises this
            # immediately, so a readiness loop must retry on FileNotFoundError.
            raise ConnectionRefusedError(f"no service at {endpoint}") from None
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        stream = _PipeStream(reader, writer)
        logger.debug("pipe: connected to %s", endpoint)
        try:
            yield stream
        finally:
            await stream.aclose()

    @override
    def create_listener(
        self, endpoint: Endpoint
    ) -> AbstractAsyncContextManager[IpcListener]:
        return self._listen(endpoint)

    @asynccontextmanager
    async def _listen(self, endpoint: Endpoint) -> AsyncGenerator[IpcListener, None]:
        loop: Any = asyncio.get_running_loop()
        path = pipe_path(endpoint)
        tx, rx = anyio.create_memory_object_stream[ByteStream](_ACCEPT_BACKLOG)

        def on_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            # Deliberately a plain function rather than a coroutine function:
            # StreamReaderProtocol only creates a task if this returns a
            # coroutine, and that task would have no owner.
            try:
                tx.send_nowait(_PipeStream(reader, writer))
            except (
                anyio.WouldBlock,
                anyio.ClosedResourceError,
                anyio.BrokenResourceError,
            ):
                logger.warning("pipe: rejecting client, accept backlog full or closed")
                writer.close()

        def factory() -> asyncio.Protocol:
            reader = asyncio.StreamReader(limit=_READ_LIMIT, loop=loop)
            return asyncio.StreamReaderProtocol(reader, on_client, loop=loop)

        try:
            servers = await loop.start_serving_pipe(factory, path)
        except PermissionError as exc:
            tx.close()
            rx.close()
            # PipeServer creates the first instance with
            # FILE_FLAG_FIRST_PIPE_INSTANCE, so a second service fails here.
            # That gives single-instance locking on Windows for free.
            raise AddressInUseError(f"address already in use: {endpoint}") from exc

        logger.info("pipe: listening on %s", endpoint)
        listener = _PipeListener(list(servers), tx, rx)
        try:
            yield listener
        finally:
            await listener.aclose()
