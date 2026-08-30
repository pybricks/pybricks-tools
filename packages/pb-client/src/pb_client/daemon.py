# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Talking to the daemon.

The shape of this API is deliberate. Nothing takes a ``timeout`` argument;
callers scope cancellation with :func:`anyio.fail_after` or
:func:`anyio.move_on_after` instead, so a deadline covers a whole operation
rather than one call inside it. Anything producing a continuing stream of events
will be an async context manager yielding an async iterator, never a callback
registration, so that no work can outlive the block that started it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from pb_client import __version__
from pb_client.autostart import open_connection
from pb_client.errors import DaemonUnavailable
from pb_core import auth, ipc
from pb_core.ipc import Endpoint
from pb_core.jsonrpc import JsonRpcPeer, connect_peer
from pb_core.protocol import PROTOCOL_VERSION

logger = logging.getLogger(__name__)

__all__ = ["Daemon", "DaemonInfo", "connect"]


@dataclass(frozen=True, slots=True)
class DaemonInfo:
    """What the daemon reports about itself."""

    version: str
    protocol_version: int
    pid: int
    uptime_s: float
    endpoint: str
    log_file: str
    idle_timeout_s: float
    clients: int

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> DaemonInfo:
        """Build from a ``daemon.info`` result.

        Args:
            payload: The result object.

        Returns:
            The parsed information.
        """
        return cls(
            version=str(payload.get("version", "unknown")),
            protocol_version=int(payload.get("protocol_version", 0)),
            pid=int(payload.get("pid", 0)),
            uptime_s=float(payload.get("uptime_s", 0.0)),
            endpoint=str(payload.get("endpoint", "")),
            log_file=str(payload.get("log_file", "")),
            idle_timeout_s=float(payload.get("idle_timeout_s", 0.0)),
            clients=int(payload.get("clients", 0)),
        )


class Daemon:
    """A connected daemon.

    Obtained from :func:`connect`; not constructed directly, because the
    connection's tasks must be owned by that context manager.
    """

    def __init__(self, peer: JsonRpcPeer, endpoint: Endpoint) -> None:
        """Wrap an established connection.

        Args:
            peer: The JSON-RPC peer, already handshaken.
            endpoint: Where this daemon is listening.
        """
        self._peer = peer
        self.endpoint = endpoint

    async def ping(self) -> None:
        """Check that the daemon is responsive."""
        await self._peer.call("daemon.ping")

    async def info(self) -> DaemonInfo:
        """Ask the daemon about itself.

        Returns:
            Its version, uptime, endpoint and client count.
        """
        return DaemonInfo.from_wire(await self._peer.call("daemon.info"))

    async def shutdown(self) -> None:
        """Ask the daemon to exit."""
        await self._peer.call("daemon.shutdown")


@asynccontextmanager
async def connect(
    endpoint: Endpoint | str | None = None, *, autostart: bool = True
) -> AsyncGenerator[Daemon, None]:
    """Connect to the daemon for the duration of the block.

    Args:
        endpoint: Where the daemon is. Defaults to the platform's own address,
            or ``PB_ENDPOINT``. Users are not expected to supply this.
        autostart: Whether to start a daemon if none is running.

    Yields:
        The connected daemon.

    Raises:
        DaemonUnavailable: If no daemon is running and ``autostart`` is false,
            or if no authentication token exists to present.
    """
    resolved = endpoint if isinstance(endpoint, Endpoint) else ipc.resolve(endpoint)

    async with open_connection(resolved, autostart=autostart) as stream:
        token = auth.load_token()
        if token is None:
            raise DaemonUnavailable(
                "no authentication token found; the daemon has not run for this user"
            )

        async with connect_peer(stream) as peer:
            # The handshake is mandatory and must come first: it authenticates
            # the connection and settles the protocol version, which matters
            # because a daemon can outlive the install that started it.
            await peer.call(
                "daemon.hello",
                {
                    "client": "pb-client",
                    "client_version": __version__,
                    "protocol_version": PROTOCOL_VERSION,
                    "token": token,
                },
            )
            logger.debug("connected to the daemon at %s", resolved)
            yield Daemon(peer, resolved)
