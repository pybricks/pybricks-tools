# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""TCP transport, for remote services and for tests.

Remote use works because compilation happens on the client: the service never
reads the caller's filesystem, so it does not need to share one.

Anything beyond loopback carries the shared secret over a plaintext socket, so
it needs TLS or an SSH tunnel in front of it. The service logs a warning when it
binds a non-loopback address.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import cast, final

import anyio
from anyio.abc import ByteStream
from typing_extensions import override

from pb_core.ipc.address import Endpoint
from pb_core.ipc.backend._anyio_listener import AnyioListener
from pb_core.ipc.backend.provider import IpcBackend, IpcListener
from pb_core.ipc.errors import AddressInUseError, is_address_in_use

logger = logging.getLogger(__name__)

__all__ = ["TcpBackend"]


def _was_refused(exc: BaseException) -> bool:
    """Whether ``exc`` ultimately reports a refused connection."""

    if isinstance(exc, ConnectionRefusedError):
        return True

    if isinstance(exc, BaseExceptionGroup):
        group = cast("BaseExceptionGroup[BaseException]", exc)
        return any(_was_refused(sub) for sub in group.exceptions)

    cause = exc.__cause__ or exc.__context__
    return _was_refused(cause) if cause is not None else False


@final
class TcpBackend(IpcBackend):
    """TCP sockets."""

    @override
    def connect(self, endpoint: Endpoint) -> AbstractAsyncContextManager[ByteStream]:
        return self._connect(endpoint)

    @asynccontextmanager
    async def _connect(self, endpoint: Endpoint) -> AsyncGenerator[ByteStream, None]:
        host, port = endpoint.tcp_address

        try:
            stream = await anyio.connect_tcp(host, port)
        except OSError as exc:
            # anyio collapses every failed attempt into a generic
            # OSError("All connection attempts failed"), so the specific
            # ConnectionRefusedError that autostart keys on is only visible in
            # the cause. Recover it, and let anything else propagate unchanged.
            if _was_refused(exc):
                raise ConnectionRefusedError(f"no service at {endpoint}") from exc

            raise

        logger.debug("tcp: connected to %s", endpoint)

        async with stream:
            yield stream

    @override
    def create_listener(
        self, endpoint: Endpoint
    ) -> AbstractAsyncContextManager[IpcListener]:
        return self._listen(endpoint)

    @asynccontextmanager
    async def _listen(self, endpoint: Endpoint) -> AsyncGenerator[IpcListener, None]:
        host, port = endpoint.tcp_address

        try:
            inner = await anyio.create_tcp_listener(local_host=host, local_port=port)
        except OSError as exc:
            if is_address_in_use(exc):
                raise AddressInUseError(f"address already in use: {endpoint}") from exc

            raise

        if not endpoint.is_loopback:
            logger.warning(
                "tcp: listening on %s, which is reachable from the network; "
                "the authentication token is sent in clear text, so use TLS or "
                "an SSH tunnel",
                endpoint,
            )
        else:
            logger.info("tcp: listening on %s", endpoint)

        listener = AnyioListener(inner)

        try:
            yield listener
        finally:
            await listener.aclose()
