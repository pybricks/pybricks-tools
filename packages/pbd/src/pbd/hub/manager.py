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
from pbd.hub.selection import Selection
from pbd.hub.transport import DeviceInfo, HubTransport, ScanFilter

logger = logging.getLogger(__name__)

__all__ = ["HubManager", "HubNotFoundError", "run_hub_manager"]


class HubNotFoundError(LookupError):
    """No hub matched."""


class HubAlreadyConnectedError(RuntimeError):
    """The hub is already connected and exclusive access was requested."""


class HubManager:
    """The set of hubs the daemon currently holds open."""

    def __init__(
        self,
        transport: HubTransport,
        task_group: TaskGroup,
        selection: Selection | None = None,
    ) -> None:
        """Create a manager.

        Args:
            transport: How hubs are reached.
            task_group: The long-lived group that will own hub connections.
                Deliberately passed in rather than created here, so that
                ownership is visible at the call site.
            selection: Which hub the user chose. Defaults to the one
                stored in the user's state directory.
        """
        self.transport = transport
        # The radio is the only thing that can notice a hub being switched
        # off or carried out of range, so what it notices has to reach the
        # connection that is waiting on it.
        transport.on_disconnect = self._transport_lost
        self._tg = task_group
        self._hubs: dict[str, HubConnection] = {}
        self._connect_locks: dict[str, anyio.Lock] = {}
        self._scan_lock = anyio.Lock()
        self._last_connected: str | None = None
        """The hub most recently connected, while it stays connected.

        The fallback for a user who has not chosen a hub and has two of
        them connected: the one just connected is the one being worked
        with. Ephemeral on purpose -- it is an id, it means nothing once
        that hub is gone, and it promises nothing.
        """

        self.selection = selection if selection is not None else Selection()
        """The hub commands act on when none is named.

        Held here rather than in a client config file so that it survives the
        command exiting and is shared between shells, and kept on disk so
        that it survives this service too. It is a name: see
        :mod:`pbd.hub.selection` for why there is nothing better to keep.
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

    def get(self, name_or_id: str | None) -> HubConnection:
        """Return a connected hub.

        Args:
            name_or_id: The hub's name or id, or ``None`` to use the chosen
                one. A name is what callers actually have: ids are built
                from the Bluetooth address, which is not stable and which
                ``pb`` no longer shows anyone, so every command that names a
                hub names it the way the user typed it.

        Returns:
            The connection.

        Raises:
            HubNotFoundError: If no such hub is connected, or nothing says
                which hub is meant.
        """
        if name_or_id is not None:
            return self.resolve(name_or_id)
        chosen = self.selection.name
        if chosen is not None:
            try:
                return self.resolve(chosen)
            except HubNotFoundError:
                # A standing preference between hubs should not stand in
                # the way of the only hub there is, so a chosen hub that
                # is switched off falls through rather than refusing.
                logger.debug("the chosen hub %r is not connected", chosen)
        last = self._last_connected
        if last is not None and last in self._hubs:
            return self._hubs[last]
        if len(self._hubs) == 1:
            # Exactly one hub is unambiguous, so do not make the user say
            # which one.
            return next(iter(self._hubs.values()))
        raise HubNotFoundError("no hub selected")

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
                    self._last_connected = device.id
                    return existing.info

            connection = HubConnection(self.transport, device)
            # start() hands the task's lifetime to this group while returning
            # the handshake result, including failures, to the caller.
            info = await self._tg.start(connection.run)
            assert isinstance(info, HubInfo)
            self._hubs[device.id] = connection
            # Connecting says which hub you mean for now, but it is not
            # the same as choosing one: pb use is what changes a
            # remembered choice, so that nothing moves it behind the
            # user's back.
            self._last_connected = device.id
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
        # The remembered choice is left alone: a hub that has been switched
        # off is still the hub the user is working with. Only the
        # ephemeral fallback moves.
        if self._last_connected == connection.id:
            self._last_connected = next(iter(self._hubs), None)
        logger.info("hub disconnected: %s (%d left)", connection.id, len(self._hubs))

    def _transport_lost(self, device_id: str) -> None:
        """Note that the transport has lost a hub.

        Runs outside any task -- on Bluetooth it is a callback from the radio
        itself -- so it does the least it can: it hands the fact to the
        connection, which wakes its own pump and unwinds in its own time.

        Args:
            device_id: The hub that went away.
        """
        connection = self._hubs.get(device_id)
        if connection is not None:
            logger.info("the transport lost %s", device_id)
            connection.notify_disconnected()

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
                if self._last_connected == hub_id:
                    self._last_connected = next(iter(self._hubs), None)

    async def close_all(self) -> None:
        """Disconnect every hub, for daemon shutdown."""
        for connection in list(self._hubs.values()):
            connection.close("daemon shutting down")
        self._hubs.clear()
        self._last_connected = None


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
