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

        # Safe because the caller holds the single-instance lock before getting
        # here: holding it proves no other daemon is serving this socket, so any
        # file present is a leftover from a crash.
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
