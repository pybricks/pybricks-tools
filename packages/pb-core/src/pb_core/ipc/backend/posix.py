# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""AF_UNIX transport for Linux and macOS.

anyio supports UNIX sockets natively, so this backend is mostly about getting
the filesystem details right: a private parent directory, mode ``0600`` on the
socket itself, and safe removal of a socket left behind by a crashed daemon.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from pathlib import Path
from typing import final

import anyio
import anyio.to_thread
from anyio.abc import ByteStream
from typing_extensions import override

from pb_core import paths
from pb_core.ipc.address import Endpoint
from pb_core.ipc.backend._anyio_listener import AnyioListener
from pb_core.ipc.backend.provider import IpcBackend, IpcListener
from pb_core.ipc.errors import AddressInUseError, is_address_in_use

logger = logging.getLogger(__name__)

__all__ = ["PosixBackend"]


@final
class PosixBackend(IpcBackend):
    """AF_UNIX sockets."""

    @override
    def connect(self, endpoint: Endpoint) -> AbstractAsyncContextManager[ByteStream]:
        return self._connect(endpoint)

    @asynccontextmanager
    async def _connect(self, endpoint: Endpoint) -> AsyncGenerator[ByteStream, None]:
        try:
            stream = await anyio.connect_unix(endpoint.target)
        except FileNotFoundError:
            # No socket file: the daemon is not running. Callers may start one.
            raise ConnectionRefusedError(f"no daemon at {endpoint}") from None
        logger.debug("unix: connected to %s", endpoint)
        async with stream:
            yield stream

    @override
    def create_listener(
        self, endpoint: Endpoint
    ) -> AbstractAsyncContextManager[IpcListener]:
        return self._listen(endpoint)

    @asynccontextmanager
    async def _listen(self, endpoint: Endpoint) -> AsyncGenerator[IpcListener, None]:
        socket = anyio.Path(endpoint.target)
        # Blocking stat/mkdir, so off the event loop thread.
        await anyio.to_thread.run_sync(paths.ensure_private_dir, Path(socket.parent))

        # A socket file on its own says nothing about whether anyone is behind
        # it, and binding would replace a live daemon's socket with ours --
        # leaving it running and unreachable. So ask: a connection that is
        # accepted is proof, and anything else means the file is debris from a
        # crash and can go. The daemon also holds the single-instance lock by
        # this point, which is the real guard; this is what makes the backend
        # honest on its own, and what makes "address already in use" mean the
        # same thing on every transport.
        if await _is_live(endpoint):
            raise AddressInUseError(f"address already in use: {endpoint}")
        with suppress(FileNotFoundError):
            await socket.unlink()

        try:
            inner = await anyio.create_unix_listener(socket, mode=0o600)
        except OSError as exc:
            if is_address_in_use(exc):
                raise AddressInUseError(f"address already in use: {endpoint}") from exc
            raise
        logger.info("unix: listening on %s", endpoint)
        listener = AnyioListener(inner)
        try:
            yield listener
        finally:
            await listener.aclose()
            with suppress(FileNotFoundError):
                await socket.unlink()


async def _is_live(endpoint: Endpoint) -> bool:
    """Whether something is already accepting connections on this socket.

    Args:
        endpoint: The socket to try.

    Returns:
        True if a connection was accepted, which only a running daemon can do.
    """
    try:
        stream = await anyio.connect_unix(endpoint.target)
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return False
    await stream.aclose()
    return True
