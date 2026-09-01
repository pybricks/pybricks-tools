# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""One hub connection, and the fan-out of its events to attached clients.

Two things here are worth understanding before changing anything.

**Buffering is asymmetric, on purpose.** The transport's notification callback
runs outside any task and must never block, so it can only ever hand data over
without waiting. Its queue is therefore unbounded, which makes that handover
incapable of failing: a status report is a *level* the service mirrors as state,
so losing one would leave the service quietly disagreeing with the hub. What
keeps unboundedness safe is explicit byte accounting -- above a high-water mark
the callback drops program output only, and counts what it dropped. Per-client
queues, by contrast, are bounded and lossy. The result is that a client which
stops reading loses its own output and nothing else; it cannot slow the radio,
and so cannot stall a robot.

**Connections outlive the clients that opened them.** A connection is started
with ``task_group.start()`` on a scope belonging to the manager, not to the
caller. The caller learns whether connecting worked, including any error from
the handshake, but does not own the running task. That is the ownership
transfer that lets a hub stay connected after the command that connected it has
exited.
"""

from __future__ import annotations

import itertools
import logging
import math
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Final

import anyio
from anyio.abc import TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from pb_core.logging import TRACE, log_context
from pb_service.hub import protocol as p
from pb_service.hub.events import (
    AppDataEvent,
    Channel,
    HubEvent,
    HubState,
    StateEvent,
    StatusEvent,
    StdoutEvent,
    StdoutRing,
    channel_of,
)
from pb_service.hub.transport import DeviceInfo, HubCommandError, HubLink, HubTransport

logger = logging.getLogger(__name__)

__all__ = ["HubConnection", "HubDisconnectedError", "HubInfo", "Subscriber"]

SUBSCRIBER_QUEUE: Final = 1024
"""Events buffered per attached client before dropping."""

INGRESS_HIGH_WATER: Final = 4 * 1024 * 1024
"""Unread hub bytes after which program output starts being discarded."""

DROP_LIMIT: Final = 10_000
"""Events a client may miss before it is disconnected as hopeless."""

_DISCONNECTED_MARKER: Final = object()


class HubDisconnectedError(RuntimeError):
    """The hub disconnected while an operation was in flight."""


@dataclass(frozen=True, slots=True)
class HubInfo:
    """What is known about a connected hub."""

    id: str
    name: str
    address: str
    transport: str
    firmware: str
    profile: p.Version
    capabilities: p.Capabilities
    pnp: p.PnpId | None = None
    """What kind of hub this is, or None if it did not say."""


@dataclass
class Subscriber:
    """One client's view of a hub's events."""

    id: str
    channels: frozenset[Channel]
    tx: MemoryObjectSendStream[HubEvent]
    rx: MemoryObjectReceiveStream[HubEvent]
    dropped: int = 0
    """Events discarded because this client was not keeping up."""

    replay: list[HubEvent] = field(default_factory=list[HubEvent])


class HubConnection:
    """A live connection to one hub."""

    def __init__(self, transport: HubTransport, device: DeviceInfo) -> None:
        """Prepare a connection. Nothing happens until it is started.

        Args:
            transport: How to reach the hub.
            device: Which hub.
        """
        self.device = device
        self.id = device.id
        self.state = HubState.DISCONNECTED
        self.info: HubInfo | None = None
        self.status: p.StatusReport | None = None

        self._transport = transport
        self._link: HubLink | None = None
        self._command_lock = anyio.Lock()
        self._closing = anyio.Event()

        self._reason = "the hub went away"
        """Why this connection is ending, for whoever is attached to it.

        Anything watching a hub is told when it goes, and the difference
        between "somebody disconnected it" and "it stopped answering" is the
        difference between a session that explains itself and one that leaves
        the user guessing. Only the log had this before.
        """

        self._closed = anyio.Event()
        self._subscribers: dict[str, Subscriber] = {}
        self._sub_ids = itertools.count(1)
        self._seq = itertools.count(1)
        self._abort_scopes: set[anyio.CancelScope] = set()
        self._scrollback = StdoutRing()

        self.stdin_limit = 1
        """How much input this hub will take in one write.

        Set from the hub's capabilities once it is connected, and lowered if
        the hub turns a write down for not fitting. Firmware that has grown its
        input buffer accepts as much as the Bluetooth link allows, which is
        what makes pasting a block of code into the interpreter quick; older
        firmware holds as little as 21 bytes and refuses anything larger
        outright. Since nothing on the wire says which, the first refusal is
        what tells us, and it is remembered so it is only paid once.
        """

        # Unbounded on purpose: see the module docstring. send_nowait on an
        # unbounded stream cannot raise, so the callback can never fail or
        # block, and a status report can never be silently lost.
        self._rx_tx, self._rx_rx = anyio.create_memory_object_stream[object](math.inf)
        self._rx_bytes = 0
        self.ingress_dropped = 0
        """Hub bytes discarded because the service fell behind. Should stay zero."""

    # -------------------------------------------------------------- lifecycle

    async def run(self, *, task_status: TaskStatus[HubInfo]) -> None:
        """Hold the connection open until asked to close.

        Started with ``task_group.start`` so the caller gets the handshake
        result while the manager keeps ownership of the task.

        Args:
            task_status: Signalled with the hub's details once connected.
        """

        with log_context(hub=self.id):
            try:
                await self._run(task_status=task_status)
            finally:
                # Both calls are synchronous, so they cannot be cancelled and
                # need no shield: the connection is always reported as gone and
                # subscribers are always released, however this task ends.
                self._set_state(HubState.DISCONNECTED, self._reason)
                self._release_subscribers()
                self._rx_tx.close()
                self._rx_rx.close()
                self._closed.set()
                logger.info("hub connection closed")

    async def _run(self, *, task_status: TaskStatus[HubInfo]) -> None:
        self._set_state(HubState.CONNECTING)
        logger.info("connecting")

        async with self._transport.open(self.device.id) as link:
            self._link = link
            info = await self._handshake(link)
            self.info = info
            self.stdin_limit = max(
                1, info.capabilities.max_write_size - p.WRITE_STDIN_HEADER_SIZE
            )
            await link.start_notify(p.PYBRICKS_COMMAND_EVENT_UUID, self._on_notify)

            async with anyio.create_task_group() as tg:
                tg.start_soon(self._pump)
                self._set_state(HubState.CONNECTED)
                logger.info(
                    "connected: %s firmware %s profile %s",
                    info.name,
                    info.firmware,
                    ".".join(str(part) for part in info.profile),
                )
                # The caller is released here, with a real result. Its own
                # scope may end immediately; this task keeps running.
                task_status.started(info)

                await self._closing.wait()
                self._set_state(HubState.DISCONNECTING, self._reason)
                tg.cancel_scope.cancel()

    async def _handshake(self, link: HubLink) -> HubInfo:
        """Read the hub's identity and capabilities."""
        name = (await link.read_characteristic(p.DEVICE_NAME_UUID)).decode(
            "utf-8", "replace"
        )
        firmware = (await link.read_characteristic(p.FIRMWARE_REVISION_UUID)).decode(
            "utf-8", "replace"
        )
        profile_text = (
            await link.read_characteristic(p.SOFTWARE_REVISION_UUID)
        ).decode("utf-8", "replace")
        profile = p.parse_version(profile_text)

        if profile < p.MIN_SUPPORTED_PROFILE:
            wanted = ".".join(str(part) for part in p.MIN_SUPPORTED_PROFILE)
            raise HubCommandError(
                p.HubError.INVALID_COMMAND,
                f"hub speaks Pybricks profile {profile_text}, but {wanted} or "
                f"later is required; update the hub's firmware",
            )

        capabilities = p.unpack_capabilities(
            await link.read_characteristic(p.PYBRICKS_HUB_CAPABILITIES_UUID)
        )

        raw_pnp = await link.read_characteristic(p.PNP_ID_UUID)

        try:
            pnp = p.unpack_pnp_id(raw_pnp)
        except ValueError:
            # Only what the hub is called is lost, so a hub that answers this
            # characteristic oddly is still perfectly usable.
            logger.warning("hub gave a PnP ID of %d bytes; ignoring", len(raw_pnp))
            pnp = None

        return HubInfo(
            id=self.id,
            name=name,
            address=self.device.address,
            transport=self.device.transport,
            firmware=firmware,
            profile=profile,
            capabilities=capabilities,
            pnp=pnp,
        )

    async def wait_closed(self) -> None:
        """Wait until this connection has finished shutting down."""
        await self._closed.wait()

    def close(self, reason: str = "requested") -> None:
        """Ask the connection to shut down.

        Args:
            reason: Why, for the log and for attached clients.
        """
        self._reason = reason
        if not self._closing.is_set():
            logger.info("closing: %s", reason)
            self._closing.set()

    # ----------------------------------------------------------- notifications

    def _on_notify(self, payload: bytes) -> None:
        """Receive one notification from the transport.

        Runs outside any task. It may not await, may not start tasks and may
        not block; it only hands the bytes over.
        """

        if logger.isEnabledFor(TRACE):
            logger.log(TRACE, "rx %s", payload.hex(" "))

        if (
            payload
            and payload[0] == p.Event.WRITE_STDOUT
            and self._rx_bytes > INGRESS_HIGH_WATER
        ):
            # Shed program output only. Status reports are state, not a
            # stream, so dropping one would desynchronise us from the hub.
            self.ingress_dropped += len(payload)
            return

        self._rx_bytes += len(payload)

        try:
            self._rx_tx.send_nowait(payload)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            logger.debug("dropping notification: the pump has stopped")

    def notify_disconnected(self) -> None:
        """Report that the transport lost the connection.

        Also invoked from outside any task, so it does the same minimal thing.
        """
        self._reason = "the hub stopped answering"

        with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
            self._rx_tx.send_nowait(_DISCONNECTED_MARKER)

        self._closing.set()

    async def _pump(self) -> None:
        """Decode notifications and fan them out. Owned by the connection."""

        async for item in self._rx_rx:
            if item is _DISCONNECTED_MARKER:
                self._abort_all("the hub disconnected")
                return

            assert isinstance(item, bytes)
            self._rx_bytes -= len(item)
            self._decode(item)

    def _decode(self, payload: bytes) -> None:
        if not payload:
            return

        kind = payload[0]

        if kind == p.Event.STATUS_REPORT:
            try:
                report = p.parse_status_report(payload)
            except ValueError:
                logger.warning("ignoring malformed status report: %s", payload.hex(" "))
                return

            self.status = report
            self._publish(StatusEvent(next(self._seq), report))
        elif kind == p.Event.WRITE_STDOUT:
            data = payload[1:]
            seq = next(self._seq)
            self._scrollback.append(seq, data)
            self._publish(StdoutEvent(seq, data))
        elif kind == p.Event.WRITE_APP_DATA:
            self._publish(AppDataEvent(next(self._seq), payload[1:]))
        else:
            # A newer hub may send events this build has never seen. Note it
            # and carry on rather than treating it as fatal.
            logger.warning("ignoring unknown hub event 0x%02x", kind)

    # ---------------------------------------------------------------- fan-out

    def _set_state(self, state: HubState, reason: str | None = None) -> None:
        if self.state is state:
            return

        logger.info("state: %s -> %s", self.state, state)
        self.state = state
        self._publish(StateEvent(next(self._seq), state, reason))

    def _publish(self, event: HubEvent) -> None:
        """Deliver an event to every interested subscriber, dropping if full."""
        channel = channel_of(event)

        for subscriber in list(self._subscribers.values()):
            if channel not in subscriber.channels:
                continue

            try:
                subscriber.tx.send_nowait(event)
            except anyio.WouldBlock:
                subscriber.dropped += 1
                if subscriber.dropped in (1, 100) or subscriber.dropped % 1000 == 0:
                    logger.warning(
                        "client %s is not keeping up; %d events dropped",
                        subscriber.id,
                        subscriber.dropped,
                    )

                if subscriber.dropped > DROP_LIMIT:
                    logger.error(
                        "client %s is hopelessly behind; detaching it",
                        subscriber.id,
                    )
                    subscriber.tx.close()
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass  # detached concurrently

    def _release_subscribers(self) -> None:
        for subscriber in list(self._subscribers.values()):
            subscriber.tx.close()

    @asynccontextmanager
    async def subscribe(
        self,
        channels: frozenset[Channel] | None = None,
        *,
        replay_bytes: int = 0,
    ) -> AsyncGenerator[Subscriber, None]:
        """Attach to this hub's events for the duration of the block.

        A context manager rather than a callback registration, so that events
        stop exactly when the block exits and no work can outlive it.

        Detaching does **not** disconnect the hub: the connection belongs to the
        service, not to whoever is listening.

        Args:
            channels: Which channels to receive. Defaults to all of them.
            replay_bytes: How much recent program output to deliver first, for
                a client attaching after a program has already started.

        Yields:
            The subscriber, whose ``rx`` is an async iterator of events.
        """
        selected = channels or frozenset(Channel)
        subscriber_id = f"s{next(self._sub_ids)}"
        tx, rx = anyio.create_memory_object_stream[HubEvent](SUBSCRIBER_QUEUE)
        subscriber = Subscriber(subscriber_id, selected, tx, rx)

        if replay_bytes and Channel.STDOUT in selected:
            for seq, data in self._scrollback.tail(replay_bytes):
                subscriber.replay.append(StdoutEvent(seq, data))

        self._subscribers[subscriber_id] = subscriber
        logger.info(
            "client attached (%s, %d now attached)",
            subscriber_id,
            len(self._subscribers),
        )

        try:
            yield subscriber
        finally:
            self._subscribers.pop(subscriber_id, None)
            tx.close()
            rx.close()
            logger.info(
                "client detached (%s, %d still attached)",
                subscriber_id,
                len(self._subscribers),
            )

    @property
    def subscriber_count(self) -> int:
        """How many clients are currently attached."""
        return len(self._subscribers)

    # ------------------------------------------------------------ cancellation

    @asynccontextmanager
    async def abort_on_disconnect(self) -> AsyncGenerator[None, None]:
        """Cancel the enclosing block if the hub disconnects.

        Replaces the older approach of racing an awaitable against a disconnect
        event and cancelling the loser by hand. A cancel scope composes by
        nesting and cannot leak a task.

        Yields:
            ``None``.

        Raises:
            HubDisconnectedError: If the hub disconnected before the block
                finished.
        """

        with anyio.CancelScope() as scope:
            self._abort_scopes.add(scope)

            try:
                yield
            finally:
                self._abort_scopes.discard(scope)

        if scope.cancel_called:
            raise HubDisconnectedError(f"{self.id} disconnected")

    def _abort_all(self, reason: str) -> None:
        if self._abort_scopes:
            logger.info("aborting %d operations: %s", len(self._abort_scopes), reason)

        for scope in list(self._abort_scopes):
            scope.cancel()

    # -------------------------------------------------------------- commands

    async def send_command(self, data: bytes) -> None:
        """Write one command to the hub, serialised against other writers.

        The lock matters because several clients share one hub: without it, one
        client's input could interleave with another's program download and
        corrupt what lands in hub memory.

        Args:
            data: The encoded command.

        Raises:
            HubDisconnectedError: If the hub is not connected.
        """
        link = self._link
        if link is None or self.state is not HubState.CONNECTED:
            raise HubDisconnectedError(f"{self.id} is not connected")

        async with self._command_lock:
            if logger.isEnabledFor(TRACE):
                logger.log(TRACE, "tx %s", data.hex(" "))

            await link.write_characteristic(
                p.PYBRICKS_COMMAND_EVENT_UUID, data, response=True
            )

    async def events(self, subscriber: Subscriber) -> AsyncIterator[HubEvent]:
        """Iterate a subscriber's events, replaying any backlog first.

        Args:
            subscriber: The subscriber to read from.

        Yields:
            Events, oldest first.
        """

        for event in subscriber.replay:
            yield event

        subscriber.replay.clear()

        async for event in subscriber.rx:
            yield event
