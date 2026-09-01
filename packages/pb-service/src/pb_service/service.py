# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The service's lifecycle.

Scope ordering is the design. The hub manager is entered *before* the
listener and exited *after* it, so hub connections live in a scope that is a
sibling of the client sessions rather than a child of any one of them. A client
disconnecting therefore cancels only its own tasks, and the hubs it was using
stay connected. That is the whole reason this process exists, and it is
structural rather than a matter of care.

Shutdown runs the same ordering in reverse: stop accepting clients, let the
existing ones unwind, then disconnect hubs cleanly, then release the lock.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

import anyio
from anyio.abc import ByteStream

from pb_core import auth, ipc
from pb_core.ipc import Endpoint
from pb_core.logging import log_startup_banner
from pb_service.hub.manager import HubManager, run_hub_manager
from pb_service.hub.transport import HubTransport

if TYPE_CHECKING:
    from pb_service.session import Session

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_IDLE_TIMEOUT", "Service", "run_service"]

DEFAULT_IDLE_TIMEOUT: Final = 300.0
"""Seconds of inactivity before an idle service exits."""


class Service:
    """Shared state for one running service."""

    def __init__(
        self,
        endpoint: Endpoint,
        token: str,
        idle_timeout: float,
        hubs: HubManager,
    ) -> None:
        """Create the service state.

        Args:
            endpoint: Where it listens.
            token: The shared secret clients must present.
            idle_timeout: Seconds with no clients and no hubs before exiting,
                or zero to run until told to stop.
            hubs: The hub manager, whose scope outlives every client session.
        """
        self.endpoint = endpoint
        self.hubs = hubs
        self.token = token
        self.idle_timeout = idle_timeout
        self.started_at = time.monotonic()
        self.sessions: set[Session] = set()
        self._last_activity = time.monotonic()
        self._shutdown = anyio.Event()
        self.shutdown_reason = "not requested"

    @property
    def uptime(self) -> float:
        """Seconds since the service started."""
        return time.monotonic() - self.started_at

    def mark_activity(self) -> None:
        """Note that something happened, deferring the idle timeout."""
        self._last_activity = time.monotonic()

    def request_shutdown(self, reason: str) -> None:
        """Ask the service to stop.

        Args:
            reason: Why, for logging and for the notification to clients.
        """

        if not self._shutdown.is_set():
            self.shutdown_reason = reason
            logger.info("shutdown requested: %s", reason)
            self._shutdown.set()

    @property
    def has_work(self) -> bool:
        """Whether anything would be lost by exiting now.

        A connected hub keeps the service alive even with no clients attached,
        because holding that connection open between commands is the point.
        """
        return bool(self.sessions) or self.hubs.count > 0

    async def _watchdog(self) -> None:
        """Exit once the service has been idle for long enough.

        It sleeps until the timeout is due rather than asking every quarter
        second whether it is. Nothing can announce that still nothing has
        happened, so a clock is the only way to know -- but a clock is not a
        poll: this wakes about once per idle period, and is exact rather than
        rounded up to the next tick.

        The quarter second it used to wake at was doing two other jobs, and
        neither survived being looked at. Sweeping the hub registry belongs to
        whatever holds the connection, which knows the moment it ends. And the
        claim that this kept the proactor loop awake enough to notice Ctrl+C
        is false on every Python this supports: asyncio sets the signal wakeup
        socket, so the C handler's byte completes the loop's own overlapped
        read and the wait returns at once. Measured on Windows 11 by sending a
        console control event to a loop parked in ``sleep_forever``: 0.7 ms
        with no tick at all, against 0.9 ms with one.
        """

        while True:
            if self.has_work:
                self.mark_activity()

            if self.idle_timeout <= 0:
                await anyio.sleep_forever()

            due = self._last_activity + self.idle_timeout
            remaining = due - time.monotonic()

            if remaining > 0:
                await anyio.sleep(remaining)

                continue

            self.request_shutdown(f"idle for {self.idle_timeout:.0f}s")

            return

    async def wait_for_shutdown(self) -> None:
        """Block until shutdown is requested."""
        await self._shutdown.wait()


DRAIN_SECONDS = 2.0
"""How long a shutdown waits for replies already queued to reach their clients.

Long enough for a reply on any working connection, short enough that a client
which has stopped reading cannot keep the service alive.
"""


@asynccontextmanager
async def run_service(
    endpoint: Endpoint,
    *,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    transport: HubTransport | None = None,
) -> AsyncGenerator[Service, None]:
    """Run a service for the duration of the block.

    Args:
        endpoint: Where to listen.
        idle_timeout: Seconds with no work before exiting, or zero for never.
        transport: How to reach hubs. Defaults to the configured backend.

    Yields:
        The running service.

    Raises:
        AddressInUseError: If another service already holds the endpoint.
    """
    from pb_service.backend import get_hub_transport
    from pb_service.session import serve_session

    token = auth.read_or_create_token()

    async with anyio.create_task_group() as root:
        # Entered before the listener and left after it. Hub connections
        # therefore live in a sibling scope of the client sessions, so a client
        # going away cancels only its own tasks and never a hub.
        async with run_hub_manager(transport or get_hub_transport()) as hubs:
            service = Service(endpoint, token, idle_timeout, hubs)
            root.start_soon(service._watchdog)  # pyright: ignore[reportPrivateUsage]

            async with ipc.create_listener(endpoint) as listener:
                logger.info("listening on %s", endpoint)

                async with anyio.create_task_group() as clients:

                    async def handle(stream: ByteStream) -> None:
                        await serve_session(service, stream)

                    clients.start_soon(listener.serve, handle, clients)

                    try:
                        yield service
                    finally:
                        # A shutdown is still a request, and its answer is the
                        # last thing these connections owe: cancelling the
                        # sessions first would drop the reply that is already
                        # queued, and the client that asked would be told the
                        # connection died rather than that it worked.
                        #
                        # Shielded, because this runs while the enclosing scope
                        # may already be cancelled, and bounded, because a
                        # client that has stopped reading must not be able to
                        # keep the service from exiting.
                        with anyio.move_on_after(DRAIN_SECONDS, shield=True):
                            for session in list(service.sessions):
                                if session.peer is not None:
                                    await session.peer.drain()

                        # Stop accepting, then let existing clients unwind.
                        # Hubs are untouched until the manager's own scope
                        # exits, below.
                        clients.cancel_scope.cancel()

            logger.info("stopped listening")

        logger.info("all hubs disconnected")
        root.cancel_scope.cancel()


def log_banner(endpoint: Endpoint, version: str) -> None:
    """Log the facts needed to interpret everything that follows.

    Args:
        endpoint: Where the service is listening.
        version: The service version.
    """
    log_startup_banner(logger, "pb-service", version, endpoint=endpoint)
