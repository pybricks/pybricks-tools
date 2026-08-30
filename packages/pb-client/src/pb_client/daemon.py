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
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from pb_client import __version__
from pb_client.autostart import open_connection
from pb_client.errors import DaemonUnavailable
from pb_core import auth, ipc
from pb_core.ipc import Endpoint
from pb_core.jsonrpc import JsonRpcPeer, Request, connect_peer, encode_binary
from pb_core.protocol import HUB_FOUND, PROTOCOL_VERSION

logger = logging.getLogger(__name__)

__all__ = ["Daemon", "DaemonInfo", "DeviceInfo", "HubInfo", "connect"]


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


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """A hub seen while scanning."""

    id: str
    name: str
    address: str
    rssi: int | None
    transport: str

    hub_type: str | None = None
    """What kind of hub it is, such as "City hub", if its advertisement says."""

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> DeviceInfo:
        """Build from a scan result entry.

        Args:
            payload: One entry from ``hub.scan``.

        Returns:
            The parsed device.
        """
        rssi = payload.get("rssi")
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            address=str(payload.get("address", "")),
            rssi=int(rssi) if isinstance(rssi, int) else None,
            transport=str(payload.get("transport", "ble")),
            hub_type=(
                str(payload["type"]) if payload.get("type") is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class HubInfo:
    """A connected hub."""

    id: str
    name: str
    state: str
    hub_type: str | None
    """What kind of hub it is, such as "Technic hub", if it is a known one."""

    firmware: str
    protocol: str
    address: str
    num_slots: int
    max_program_size: int
    subscribers: int
    program_running: bool
    status_names: tuple[str, ...]

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> HubInfo:
        """Build from a hub description.

        Args:
            payload: A hub object from ``hub.list``, ``hub.info`` or
                ``hub.connect``.

        Returns:
            The parsed hub.
        """
        names = payload.get("status_names") or []
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            state=str(payload.get("state", "unknown")),
            hub_type=(
                str(payload["type"]) if payload.get("type") is not None else None
            ),
            firmware=str(payload.get("firmware", "")),
            protocol=str(payload.get("protocol", "")),
            address=str(payload.get("address", "")),
            num_slots=int(payload.get("num_slots", 0)),
            max_program_size=int(payload.get("max_program_size", 0)),
            subscribers=int(payload.get("subscribers", 0)),
            program_running=bool(payload.get("program_running", False)),
            status_names=tuple(str(name) for name in names),
        )


async def _devices(
    payloads: MemoryObjectReceiveStream[dict[str, Any]],
) -> AsyncIterator[DeviceInfo]:
    """Parse scan notifications as they arrive.

    Args:
        payloads: The raw notification parameters.

    Yields:
        One device per advertisement.
    """
    async with payloads:
        async for payload in payloads:
            yield DeviceInfo.from_wire(payload)


class _Notifications:
    """Hands each of the daemon's notifications to whoever is listening.

    One of these is created before the connection, because the peer needs a
    handler at the moment it starts reading and the :class:`Daemon` that will
    use it does not exist yet.

    Delivery is lossy on purpose. A subscriber that stops reading must not
    stall the single reader task that serves the whole connection, including
    every reply every other call is waiting for, so a full queue drops the
    event instead of applying backpressure. Nothing a notification carries is
    unrecoverable: the scan's own result still lists every hub that was seen.
    """

    def __init__(self) -> None:
        """Start with nobody listening."""
        self._listeners: dict[str, set[MemoryObjectSendStream[dict[str, Any]]]] = {}

    def add(self, method: str, stream: MemoryObjectSendStream[dict[str, Any]]) -> None:
        """Start sending one method's notifications to a stream.

        Args:
            method: The notification name.
            stream: Where to put them.
        """
        self._listeners.setdefault(method, set()).add(stream)

    def discard(
        self, method: str, stream: MemoryObjectSendStream[dict[str, Any]]
    ) -> None:
        """Stop sending to a stream.

        Args:
            method: The notification name.
            stream: The stream to forget. Not being registered is fine.
        """
        self._listeners.get(method, set()).discard(stream)

    async def handle(self, request: Request) -> None:
        """Deliver one inbound notification.

        Args:
            request: What arrived. Its id is unused: notifications have none.
        """
        # A copy, because delivering can close a stream and so change the set.
        for stream in list(self._listeners.get(request.method, ())):
            try:
                stream.send_nowait(request.params or {})
            except (
                anyio.WouldBlock,
                anyio.BrokenResourceError,
                anyio.ClosedResourceError,
            ):
                logger.debug("dropped a %s: nobody reading", request.method)


class Daemon:
    """A connected daemon.

    Obtained from :func:`connect`; not constructed directly, because the
    connection's tasks must be owned by that context manager.
    """

    def __init__(
        self,
        peer: JsonRpcPeer,
        endpoint: Endpoint,
        notifications: _Notifications | None = None,
    ) -> None:
        """Wrap an established connection.

        Args:
            peer: The JSON-RPC peer, already handshaken.
            endpoint: Where this daemon is listening.
            notifications: The router the peer was given. Without one the
                subscription methods have nothing to read, so they refuse
                rather than waiting forever.
        """
        self._peer = peer
        self.endpoint = endpoint
        self._notifications = notifications

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

    async def scan(
        self, *, seconds: float | None = 10.0, name: str | None = None
    ) -> list[DeviceInfo]:
        """Look for hubs that are advertising.

        Args:
            seconds: How long the daemon should look, or ``None`` for as long
                as this call is waiting. This bounds the scan itself rather
                than the call; wrap the call in a cancel scope to bound the
                wait.
            name: Only report hubs with this name.

        Returns:
            The hubs seen.
        """
        params: dict[str, Any] = {"timeout_s": seconds}
        if name:
            params["name"] = name
        result = await self._peer.call("hub.scan", params)
        return [DeviceInfo.from_wire(item) for item in result.get("devices", [])]

    @asynccontextmanager
    async def scanning(
        self, *, seconds: float | None = 10.0, name: str | None = None
    ) -> AsyncGenerator[AsyncIterator[DeviceInfo], None]:
        """Scan, reporting each hub as it is seen rather than at the end.

        The iterator yields one item per advertisement, so the same hub arrives
        many times over a scan with its signal strength as it is now. That is
        the point: a hub's signal is how someone works out which of two
        identically named hubs is the one on the desk in front of them. Callers
        that only want a list should use :meth:`scan`.

        Leaving the block early stops the scan, so a caller can take the first
        hub it likes and get on with it.

        Args:
            seconds: How long the daemon should look, or ``None`` to keep
                looking until the block is left. Use ``None`` when a person is
                reading the results, since a deadline on a person only ever
                expires at the worst moment.
            name: Only report hubs with this name.

        Yields:
            An iterator over the hubs seen, ending when the scan does -- which,
            with no time limit, means when the block is left.

        Raises:
            RuntimeError: If this connection has no notification router, which
                would leave the iterator silently empty.
        """
        if self._notifications is None:
            raise RuntimeError("this connection does not receive notifications")

        params: dict[str, Any] = {"timeout_s": seconds}
        if name:
            params["name"] = name

        # Bounded, and generously: an advertisement every few milliseconds from
        # several hubs still leaves room for a consumer redrawing a table.
        sender, receiver = anyio.create_memory_object_stream[dict[str, Any]](256)
        self._notifications.add(HUB_FOUND, sender)

        async def scan_until_done() -> None:
            try:
                await self._peer.call("hub.scan", params)
            finally:
                # The scan is over, so end the caller's loop rather than
                # leaving it waiting for a hub that is not coming.
                sender.close()

        async with anyio.create_task_group() as tg:
            tg.start_soon(scan_until_done)
            devices = _devices(receiver)
            try:
                # Closed before the task group is cancelled, so a caller that
                # stops after the first hub leaves no generator suspended
                # mid-iteration for the collector to complain about later.
                async with aclosing(devices):
                    yield devices
            finally:
                self._notifications.discard(HUB_FOUND, sender)
                # Whether the caller finished or walked away, nothing this
                # method started outlives the block.
                tg.cancel_scope.cancel()

    async def connect_hub(
        self, *, hub_id: str | None = None, name: str | None = None
    ) -> HubInfo:
        """Connect to a hub and leave it connected.

        The connection outlives this client: that is the point of the daemon.

        Args:
            hub_id: The hub's id, from :meth:`scan`.
            name: Connect to the first hub with this name instead.

        Returns:
            The connected hub.
        """
        params: dict[str, Any] = {}
        if hub_id:
            params["id"] = hub_id
        if name:
            params["name"] = name
        return HubInfo.from_wire(await self._peer.call("hub.connect", params))

    async def disconnect_hub(self, hub_id: str | None = None) -> None:
        """Disconnect a hub.

        Args:
            hub_id: Which hub, or ``None`` for the current one.
        """
        await self._peer.call("hub.disconnect", {"id": hub_id} if hub_id else {})

    async def list_hubs(self) -> tuple[list[HubInfo], str | None]:
        """List the hubs the daemon is holding open.

        Returns:
            The connected hubs, and the id of the current default.
        """
        result = await self._peer.call("hub.list")
        hubs = [HubInfo.from_wire(item) for item in result.get("hubs", [])]
        return hubs, result.get("default")

    async def hub_info(self, hub_id: str | None = None) -> HubInfo:
        """Describe one hub.

        Args:
            hub_id: Which hub, or ``None`` for the current one.

        Returns:
            The hub's details and status.
        """
        return HubInfo.from_wire(
            await self._peer.call("hub.info", {"id": hub_id} if hub_id else {})
        )

    async def use_hub(self, hub_id: str | None) -> str | None:
        """Choose the hub later commands act on by default.

        Args:
            hub_id: The hub to select, or ``None`` to clear the selection.

        Returns:
            The selected hub id.
        """
        result = await self._peer.call("hub.use", {"id": hub_id})
        return result.get("default")

    async def download(self, program: bytes, *, hub_id: str | None = None) -> int:
        """Send a compiled program to a hub without starting it.

        Args:
            program: The compiled program.
            hub_id: Which hub, or ``None`` for the current one.

        Returns:
            The number of bytes written.
        """
        params: dict[str, Any] = {"mpy": encode_binary(program)}
        if hub_id:
            params["id"] = hub_id
        result = await self._peer.call("program.download", params)
        return int(result.get("size", 0))

    async def run_program(
        self,
        program: bytes,
        *,
        hub_id: str | None = None,
        slot: int | None = None,
        wait: bool = True,
    ) -> str:
        """Download a program and run it.

        There is no timeout argument: wrap the call in a cancel scope. Doing so
        also stops the program on the hub, rather than abandoning it.

        Args:
            program: The compiled program.
            hub_id: Which hub, or ``None`` for the current one.
            slot: Which program slot to use.
            wait: Wait for the program to finish before returning.

        Returns:
            Why the run ended: ``started``, ``stopped`` or ``disconnected``.
        """
        params: dict[str, Any] = {"mpy": encode_binary(program), "wait": wait}
        if hub_id:
            params["id"] = hub_id
        if slot is not None:
            params["slot"] = slot
        result = await self._peer.call("program.run", params)
        return str(result.get("reason", "stopped"))

    async def start_program(
        self,
        *,
        hub_id: str | None = None,
        slot: int | None = None,
        wait: bool = False,
    ) -> str:
        """Start a program already stored on the hub.

        Args:
            hub_id: Which hub, or ``None`` for the current one.
            slot: Which slot, or ``None`` for the hub's current selection.
            wait: Whether to return only once the program has stopped.

        Returns:
            Why it ended: ``"started"`` if not waiting, otherwise ``"stopped"``
            or ``"disconnected"``.
        """
        params: dict[str, Any] = {"wait": wait}
        if hub_id:
            params["id"] = hub_id
        if slot is not None:
            params["slot"] = slot
        result = await self._peer.call("program.start", params)
        return str(result.get("reason", "started"))

    async def stop_program(self, hub_id: str | None = None) -> None:
        """Stop the program running on a hub.

        Args:
            hub_id: Which hub, or ``None`` for the current one.
        """
        await self._peer.call("program.stop", {"id": hub_id} if hub_id else {})


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
                "no saved credentials; the background service has not run for this user yet"
            )

        # The router has to exist before the peer starts reading, so it is
        # made here and handed to both.
        notifications = _Notifications()
        async with connect_peer(stream, notifications.handle) as peer:
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
            yield Daemon(peer, resolved, notifications)
