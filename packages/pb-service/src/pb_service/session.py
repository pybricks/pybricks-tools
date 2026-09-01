# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""One connected client.

A session owns nothing that outlives it except by explicit hand-off. When a
client disconnects, its subscriptions unwind, but hub connections do not: they
belong to a longer-lived scope, which is what lets a hub stay connected between
commands.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import anyio
from anyio.abc import ByteStream

from pb_core.jsonrpc import (
    ErrorCode,
    JsonRpcError,
    JsonRpcPeer,
    Request,
    connect_peer,
)
from pb_core.logging import log_context
from pb_service.methods import METHODS

if TYPE_CHECKING:
    from pb_service.service import Service

logger = logging.getLogger(__name__)

__all__ = ["Session", "serve_session"]


class Session:
    """State for one client connection."""

    _next_id = 0

    def __init__(self, service: Service) -> None:
        """Create a session.

        Args:
            service: The service this client is talking to.
        """
        Session._next_id += 1
        self.id = f"c{Session._next_id}"
        self.service = service
        self.authenticated = False
        self.client_name = "unknown"
        self.client_version = "unknown"
        self.peer: JsonRpcPeer | None = None

    async def dispatch(self, request: Request) -> Any:
        """Route one request to its registered method.

        Args:
            request: The inbound request.

        Returns:
            The method's result.

        Raises:
            JsonRpcError: If the method is unknown, the handshake has not
                happened yet, or the method itself fails.
        """
        entry = METHODS.get(request.method)
        if entry is None:
            raise JsonRpcError(
                ErrorCode.METHOD_NOT_FOUND, f"unknown method: {request.method}"
            )

        if entry.needs_hello and not self.authenticated:
            raise JsonRpcError(
                ErrorCode.HELLO_REQUIRED,
                "service.hello must be the first call on a connection",
            )

        return await entry.func(self, request.params or {})


async def serve_session(service: Service, stream: ByteStream) -> None:
    """Serve one client until it disconnects.

    Args:
        service: The service.
        stream: The client's transport.
    """
    session = Session(service)

    with log_context(client=session.id):
        logger.info("client connected")
        service.sessions.add(session)

        try:
            async with connect_peer(stream, session.dispatch) as peer:
                session.peer = peer
                await peer.wait_closed()
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            logger.debug("client connection failed")
        finally:
            service.sessions.discard(session)
            service.mark_activity()
            logger.info(
                "client disconnected (%s %s)",
                session.client_name,
                session.client_version,
            )
