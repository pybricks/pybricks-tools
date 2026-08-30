# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Owns every hub connection for the lifetime of the daemon.

The manager's task group is entered before the listener and left after it, so
hub connections sit in a scope that is a sibling of the client sessions rather
than a child of any one of them. A client disconnecting cancels only its own
tasks. This is what makes "the hub stays connected between commands" a property
of the structure rather than something to remember.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager

import anyio
from anyio.abc import TaskGroup

from pbd.hub.connection import HubConnection, HubInfo
from pbd.hub.events import HubState
from pbd.hub.transport import DeviceInfo, HubTransport, ScanFilter

logger = logging.getLogger(__name__)

__all__ = ["HubManager", "HubNotFoundError", "run_hub_manager"]


class HubNotFoundError(LookupError):
    """No hub matched."""


class HubAlreadyConnectedError(RuntimeError):
    """The hub is already connected and exclusive access was requested."""


class HubManager:
    """The set of hubs the daemon currently holds open."""

    def __init__(self, transport: HubTransport, task_group: TaskGroup) -> None:
        """Create a manager.

        Args:
            transport: How hubs are reached.
            task_group: The long-lived group that will own hub connections.
                Deliberately passed in rather than created here, so that
                ownership is visible at the call site.
        """
        self.transport = transport
        self._tg = task_group
        self._hubs: dict[str, HubConnection] = {}
        self._connect_locks: dict[str, anyio.Lock] = {}
        self._scan_lock = anyio.Lock()
        self.default_hub_id: str | None = None
        """The hub commands act on when none is named.

        Held here rather than in a client config file so that it survives the
        command exiting and is shared between shells.
        """

    # ------------------------------------------------------------------ query

    def __contains__(self, hub_id: str) -> bool:
        """Whether a hub is currently connected."""
        return hub_id in self._hubs

    @property
    def connected(self) -> list[HubConnection]:
        """Every hub currently connected."""
        return list(self._hubs.values())

    @property
    def count(self) -> int:
        """How many hubs are connected."""
        return len(self._hubs)

    def get(self, hub_id: str | None) -> HubConnection:
        """Return a connected hub.

        Args:
            hub_id: The hub's id, or ``None`` to use the current default.

        Returns:
            The connection.

        Raises:
            HubNotFoundError: If no such hub is connected, or no default is set.
        """
        wanted = hub_id or self.default_hub_id
        if wanted is None:
            if len(self._hubs) == 1:
                # Exactly one hub is unambiguous, so do not make the user say
                # which one.
                return next(iter(self._hubs.values()))
            raise HubNotFoundError(
                "no hub selected; connect one first, or name it explicitly"
            )
        connection = self._hubs.get(wanted)
        if connection is None:
            raise HubNotFoundError(f"hub is not connected: {wanted}")
        return connection

    def resolve(self, name_or_id: str) -> HubConnection:
        """Find a connected hub by id or by name.

        Args:
            name_or_id: An id, or a hub's advertised name.

        Returns:
            The connection.

        Raises:
            HubNotFoundError: If nothing matches.
        """
        if name_or_id in self._hubs:
            return self._hubs[name_or_id]
        for connection in self._hubs.values():
            if connection.info is not None and connection.info.name == name_or_id:
                return connection
        raise HubNotFoundError(f"no connected hub named {name_or_id!r}")

    # ------------------------------------------------------------------ scan

    async def discover(
        self, scan_filter: ScanFilter | None = None
    ) -> AsyncIterator[DeviceInfo]:
        """Report hubs as they are seen, until the caller stops listening.

        Only one scan runs at a time, since the radio cannot do more. Callers
        arriving during a scan wait for it rather than failing.

        Args:
            scan_filter: Which hubs to report.

        Yields:
            Hubs as their advertisements arrive.
        """
        async with self._scan_lock:
            logger.info("scanning for hubs")
            seen = 0
            try:
                async with aclosing(
                    self.transport.discover(scan_filter or ScanFilter())
                ) as stream:
                    async for device in stream:
                        seen += 1
                        yield device
            finally:
                logger.info("scan stopped after %d advertisement(s)", seen)

    # --------------------------------------------------------------- connect

    async def connect(self, device: DeviceInfo, *, exclusive: bool = False) -> HubInfo:
        """Connect to a hub, or return the existing connection.

        Args:
            device: The hub to connect to.
            exclusive: Fail rather than sharing an existing connection.

        Returns:
            The hub's details.

        Raises:
            HubAlreadyConnectedError: If it is connected and ``exclusive``.
        """
        # Per-hub lock: two clients connecting to the same hub at once must not
        # both start a connection, which on Bluetooth would be a real mess.
        lock = self._connect_locks.setdefault(device.id, anyio.Lock())
        async with lock:
            existing = self._hubs.get(device.id)
            if existing is not None:
                if exclusive:
                    raise HubAlreadyConnectedError(f"{device.id} is already connected")
                if existing.info is not None:
                    logger.debug("reusing the existing connection to %s", device.id)
                    self.default_hub_id = device.id
                    return existing.info

            connection = HubConnection(self.transport, device)
            # start() hands the task's lifetime to this group while returning
            # the handshake result, including failures, to the caller.
            info = await self._tg.start(connection.run)
            assert isinstance(info, HubInfo)
            self._hubs[device.id] = connection
            self.default_hub_id = device.id
            logger.info("hub connected: %s (%d total)", device.id, len(self._hubs))
            return info

    async def disconnect(self, hub_id: str) -> None:
        """Disconnect a hub and forget it.

        Args:
            hub_id: Which hub.

        Raises:
            HubNotFoundError: If it is not connected.
        """
        connection = self.get(hub_id)
        connection.close("client requested disconnect")
        self._hubs.pop(connection.id, None)
        if self.default_hub_id == connection.id:
            self.default_hub_id = next(iter(self._hubs), None)
        logger.info("hub disconnected: %s (%d left)", connection.id, len(self._hubs))

    def forget_closed(self) -> None:
        """Drop connections that have gone away on their own.

        A hub can disconnect without being asked -- powered off, out of range
        -- so the registry needs sweeping rather than only being edited by
        request.
        """
        for hub_id, connection in list(self._hubs.items()):
            if connection.state is HubState.DISCONNECTED:
                logger.info("hub went away: %s", hub_id)
                self._hubs.pop(hub_id, None)
                if self.default_hub_id == hub_id:
                    self.default_hub_id = next(iter(self._hubs), None)

    async def close_all(self) -> None:
        """Disconnect every hub, for daemon shutdown."""
        for connection in list(self._hubs.values()):
            connection.close("daemon shutting down")
        self._hubs.clear()
        self.default_hub_id = None


@asynccontextmanager
async def run_hub_manager(transport: HubTransport) -> AsyncGenerator[HubManager, None]:
    """Run a hub manager, and its connections, for the duration of the block.

    Args:
        transport: How hubs are reached.

    Yields:
        The manager.
    """
    async with anyio.create_task_group() as tg:
        manager = HubManager(transport, tg)
        try:
            yield manager
        finally:
            # Ask every hub to close, give the connections a moment to report
            # DISCONNECTED and run their own cleanup, then cancel whatever is
            # left. Shielded, because shutdown is often itself a cancellation.
            closing = manager.connected
            with anyio.CancelScope(shield=True), anyio.move_on_after(3):
                await manager.close_all()
                for connection in closing:
                    await connection.wait_closed()
            tg.cancel_scope.cancel()
