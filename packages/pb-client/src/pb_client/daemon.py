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
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from pb_client import __version__, stdio
from pb_client.autostart import open_connection
from pb_client.errors import DaemonTooOld, DaemonUnavailable
from pb_client.models import (
    Choice,
    DaemonInfo,
    DeviceInfo,
    Downloaded,
    HubInfo,
)
from pb_client.stdio import HubEvent
from pb_core import auth, ipc
from pb_core.ipc import Endpoint
from pb_core.jsonrpc import JsonRpcPeer, Request, connect_peer, encode_binary
from pb_core.protocol import (
    HUB_FOUND,
    HUB_STATE,
    HUB_STATUS,
    HUB_STDOUT,
    PROTOCOL_VERSION,
    STDIO_CAPABILITY,
)

logger = logging.getLogger(__name__)

__all__ = ["Daemon", "DaemonInfo", "DeviceInfo", "HubInfo", "connect"]


Listener = MemoryObjectSendStream[tuple[str, dict[str, Any]]]
"""Where a subscriber's notifications are put.

Each carries the method that delivered it as well as its parameters, so that a
subscriber to more than one kind can tell them apart without guessing from the
payload -- and, more importantly, can see them all in the order they arrived.
"""


async def _devices(
    payloads: MemoryObjectReceiveStream[tuple[str, dict[str, Any]]],
) -> AsyncIterator[DeviceInfo]:
    """Parse scan notifications as they arrive.

    Args:
        payloads: The raw notifications.

    Yields:
        One device per advertisement.
    """
    async with payloads:
        async for _method, payload in payloads:
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
        self._listeners: dict[str, set[Listener]] = {}

    def add(self, method: str, stream: Listener) -> None:
        """Start sending one method's notifications to a stream.

        Args:
            method: The notification name.
            stream: Where to put them.
        """
        self._listeners.setdefault(method, set()).add(stream)

    def discard(self, method: str, stream: Listener) -> None:
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
                stream.send_nowait((request.method, request.params or {}))
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
        capabilities: frozenset[str] = frozenset(),
    ) -> None:
        """Wrap an established connection.

        Args:
            peer: The JSON-RPC peer, already handshaken.
            endpoint: Where this daemon is listening.
            notifications: The router the peer was given. Without one the
                subscription methods have nothing to read, so they refuse
                rather than waiting forever.
            capabilities: What the daemon said it can do, from the handshake.
        """
        self._peer = peer
        self.endpoint = endpoint
        self._notifications = notifications

        self.capabilities = capabilities
        """What this daemon can do.

        Not the same as what this build knows how to ask for: the service
        outlives the install that started it, so a feature added since then has
        to be asked about rather than assumed.
        """

    def _require(self, capability: str, feature: str) -> None:
        """Check that the daemon has a capability before relying on it.

        Args:
            capability: The name as the handshake reports it.
            feature: What the caller wanted, phrased to follow "cannot".

        Raises:
            DaemonTooOld: If the daemon did not advertise it.
        """
        if capability not in self.capabilities:
            raise DaemonTooOld(feature)

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
        sender, receiver = anyio.create_memory_object_stream[
            tuple[str, dict[str, Any]]
        ](256)
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
            The connected hubs, and the id of the one a command with no
            hub named would act on. That is the chosen hub when it is
            connected, and otherwise whichever hub would answer
            instead; :meth:`chosen_hub` reports the choice itself.
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

    async def use_hub(self, hub: str | None) -> Choice:
        """Choose the hub later commands act on when none is named.

        The choice outlives the service, so it is a name: a hub has
        nothing durable to be remembered by, and an id built from a
        Bluetooth address would stop naming it. A hub that is not
        connected can be chosen by name; an id can only be given for
        a hub that is, since there is otherwise no name to store.

        Args:
            hub: The hub to choose, by name or by id, or ``None`` to
                forget the chosen one.

        Returns:
            What was chosen, and whether it is connected.
        """
        result = await self._peer.call("hub.use", {"id": hub})
        return Choice.from_wire(result)

    async def chosen_hub(self) -> Choice:
        """Report the chosen hub without changing it.

        Asked of ``hub.list`` rather than of ``hub.use``, which would be
        the tidier place for it. A service outlives the install that
        started it, so a client is quite likely to be talking to
        yesterday's: an older ``hub.use`` reads a request with no hub in
        it as a request to forget the chosen one, and a question that
        silently answers by throwing away the answer is not a question
        worth asking. An older ``hub.list`` merely omits the field.

        Returns:
            What was chosen, and whether it is connected. The name is
            None when nothing has been chosen.
        """
        result = await self._peer.call("hub.list")
        chosen = result.get("chosen")
        if not isinstance(chosen, str) or not chosen:
            return Choice(name=None, connected=False)
        connected = any(
            item.get("name") == chosen
            for item in result.get("hubs", [])
            if isinstance(item, dict)
        )
        return Choice(name=chosen, connected=connected)

    async def download(
        self,
        program: bytes,
        *,
        hub_id: str | None = None,
        slot: int | None = None,
    ) -> Downloaded:
        """Send a compiled program to a hub without starting it.

        Args:
            program: The compiled program.
            hub_id: Which hub, or ``None`` for the current one.
            slot: The slot it is expected to land in, or ``None`` to accept
                whichever the hub has selected. The hub decides where a
                program goes, so naming one that is not selected is refused
                rather than quietly ignored.

        Returns:
            What was written and where.
        """
        params: dict[str, Any] = {"mpy": encode_binary(program)}
        if hub_id:
            params["id"] = hub_id
        if slot is not None:
            params["slot"] = slot
        result = await self._peer.call("program.download", params)
        return Downloaded.from_wire(result)

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

    async def start_repl(self, hub_id: str | None = None) -> None:
        """Start the hub's built-in interactive interpreter.

        What runs afterwards is an ordinary user program as far as the hub is
        concerned, so it stops the same way anything else does.

        Args:
            hub_id: Which hub, or ``None`` for the current one.
        """
        self._require(STDIO_CAPABILITY, "run the interpreter")
        await self._peer.call("program.repl", {"id": hub_id} if hub_id else {})

    async def write_stdin(self, data: bytes, *, hub_id: str | None = None) -> None:
        """Send input to whatever is running on a hub.

        Returns once the hub has taken every byte. That can mean waiting: a hub
        whose input buffer is full refuses a write rather than keeping part of
        it, and the daemon waits for the program to read rather than dropping
        what it was given.

        Args:
            data: The bytes to send, exactly as they should arrive.
            hub_id: Which hub, or ``None`` for the current one.
        """
        self._require(STDIO_CAPABILITY, "send input to a hub")
        params: dict[str, Any] = {"data": encode_binary(data)}
        if hub_id:
            params["id"] = hub_id
        await self._peer.call("hub.stdin", params)

    @asynccontextmanager
    async def attach(
        self, *, hub_id: str | None = None, replay_bytes: int = 0
    ) -> AsyncGenerator[AsyncIterator[HubEvent], None]:
        """Follow everything a hub says, for the duration of the block.

        Output, status and connection state arrive on one iterator, in the
        order they happened, which is what a terminal needs: a status saying
        the program stopped must not overtake the last thing it printed.

        Attaching disturbs nobody. Several clients can watch one hub at once,
        and leaving does not stop what is running.

        Args:
            hub_id: Which hub, or ``None`` for the current one.
            replay_bytes: How much output from before attaching to deliver
                first. With a service that outlives commands, the usual thing
                is to start a program in one window and attach in another a
                moment later, and without this that program's first lines are
                lost every time.

        Yields:
            The hub's events, ending when the hub disconnects or the block is
            left.

        Raises:
            RuntimeError: If this connection has no notification router, which
                would leave the iterator silently empty.
        """
        self._require(STDIO_CAPABILITY, "carry a hub's input and output")
        if self._notifications is None:
            raise RuntimeError("this connection does not receive notifications")

        params: dict[str, Any] = {"replay_bytes": replay_bytes}
        if hub_id:
            params["id"] = hub_id

        # Bounded, and lossy if the consumer stalls: see _Notifications. A
        # terminal that stops reading its own output for this long has larger
        # problems than a gap in it, and the alternative -- backpressure into
        # the one reader task -- would stall every other call on this
        # connection, including the ones being typed.
        sender, receiver = anyio.create_memory_object_stream[
            tuple[str, dict[str, Any]]
        ](1024)
        for name in (HUB_STDOUT, HUB_STATUS, HUB_STATE):
            self._notifications.add(name, sender)

        async def attached_until_done() -> None:
            try:
                await self._peer.call("hub.attach", params)
            finally:
                # The hub has gone. End the caller's loop rather than leaving
                # it waiting for output that is not coming.
                sender.close()

        async with anyio.create_task_group() as tg:
            tg.start_soon(attached_until_done)
            stream = stdio.events(receiver)
            try:
                # Closed before the group is cancelled, so a caller that stops
                # early leaves no generator suspended mid-iteration.
                async with aclosing(stream):
                    yield stream
            finally:
                for name in (HUB_STDOUT, HUB_STATUS, HUB_STATE):
                    self._notifications.discard(name, sender)
                tg.cancel_scope.cancel()


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
            hello = await peer.call(
                "daemon.hello",
                {
                    "client": "pb-client",
                    "client_version": __version__,
                    "protocol_version": PROTOCOL_VERSION,
                    "token": token,
                },
            )
            capabilities = frozenset(
                str(name) for name in hello.get("capabilities", ())
            )
            logger.debug(
                "connected to the daemon at %s (capabilities: %s)",
                resolved,
                ", ".join(sorted(capabilities)) or "none",
            )
            yield Daemon(peer, resolved, notifications, capabilities)
