# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""An in-process transport, used by the test suite.

Running the whole service and client stack in one process with no OS resources
makes the integration tests fast and identical on every platform, which leaves
the real per-platform backends needing only a conformance suite.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import ClassVar, final

import anyio
from anyio.abc import ByteStream, TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.streams.stapled import StapledObjectStream
from typing_extensions import override

from pb_core.ipc.address import Endpoint
from pb_core.ipc.backend.provider import IpcBackend, IpcListener, StreamHandler, owned
from pb_core.ipc.errors import AddressInUseError

logger = logging.getLogger(__name__)

__all__ = ["MemoryBackend"]

_BUFFER = 64


@final
class _MemoryByteStream(ByteStream):
    """Adapts an object stream of ``bytes`` to the byte-stream interface.

    A byte stream must honour ``max_bytes``, so anything left over from a
    received chunk is buffered until the next call.
    """

    def __init__(self, inner: StapledObjectStream[bytes]) -> None:
        self._inner = inner
        self._pending = b""

    @override
    async def receive(self, max_bytes: int = 65536) -> bytes:
        if not self._pending:
            self._pending = await self._inner.receive()
        chunk, self._pending = self._pending[:max_bytes], self._pending[max_bytes:]
        return chunk

    @override
    async def send(self, item: bytes) -> None:
        await self._inner.send(item)

    @override
    async def send_eof(self) -> None:
        await self._inner.send_eof()

    @override
    async def aclose(self) -> None:
        await self._inner.aclose()


def _pair() -> tuple[ByteStream, ByteStream]:
    """Create two byte streams connected to each other."""
    a_tx, b_rx = anyio.create_memory_object_stream[bytes](_BUFFER)
    b_tx, a_rx = anyio.create_memory_object_stream[bytes](_BUFFER)
    return (
        _MemoryByteStream(StapledObjectStream(a_tx, a_rx)),
        _MemoryByteStream(StapledObjectStream(b_tx, b_rx)),
    )


@final
class _MemoryListener(IpcListener):
    """Accepts connections handed over through a queue."""

    def __init__(
        self,
        key: str,
        tx: MemoryObjectSendStream[ByteStream],
        rx: MemoryObjectReceiveStream[ByteStream],
    ) -> None:
        self.key = key
        self.tx = tx
        self._rx = rx

    @override
    async def serve(self, handler: StreamHandler, task_group: TaskGroup) -> None:
        wrapped = owned(handler)
        async for stream in self._rx:
            task_group.start_soon(wrapped, stream)

    @override
    async def aclose(self) -> None:
        self.tx.close()
        # Close anything accepted but never served, or it leaks on shutdown.
        with suppress(anyio.WouldBlock, anyio.EndOfStream, anyio.ClosedResourceError):
            while True:
                await self._rx.receive_nowait().aclose()
        self._rx.close()


@final
class MemoryBackend(IpcBackend):
    """A process-global registry of in-process listeners keyed by name."""

    _listeners: ClassVar[dict[str, _MemoryListener]] = {}

    @override
    def connect(self, endpoint: Endpoint) -> AbstractAsyncContextManager[ByteStream]:
        return self._do_connect(endpoint)

    @asynccontextmanager
    async def _do_connect(self, endpoint: Endpoint) -> AsyncGenerator[ByteStream, None]:
        listener = self._listeners.get(endpoint.target)
        if listener is None:
            raise ConnectionRefusedError(f"no listener at {endpoint}")
        client, server = _pair()
        try:
            listener.tx.send_nowait(server)
        except (anyio.WouldBlock, anyio.ClosedResourceError, anyio.BrokenResourceError):
            await client.aclose()
            await server.aclose()
            raise ConnectionRefusedError(
                f"listener at {endpoint} is not accepting"
            ) from None
        logger.debug("memory: connected to %s", endpoint)
        async with client:
            yield client

    @override
    def create_listener(
        self, endpoint: Endpoint
    ) -> AbstractAsyncContextManager[IpcListener]:
        return self._do_listen(endpoint)

    @asynccontextmanager
    async def _do_listen(self, endpoint: Endpoint) -> AsyncGenerator[IpcListener, None]:
        if endpoint.target in self._listeners:
            raise AddressInUseError(f"address already in use: {endpoint}")
        tx, rx = anyio.create_memory_object_stream[ByteStream](_BUFFER)
        listener = _MemoryListener(endpoint.target, tx, rx)
        self._listeners[endpoint.target] = listener
        logger.debug("memory: listening on %s", endpoint)
        try:
            yield listener
        finally:
            del self._listeners[endpoint.target]
            await listener.aclose()
