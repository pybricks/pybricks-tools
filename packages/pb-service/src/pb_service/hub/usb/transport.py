# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""USB transport: a hub reached as an ordinary serial port.

The firmware presents a hub as a standard CDC ACM device, so this needs no
driver anywhere -- Windows' own ``usbser.sys``, ``/dev/ttyACM*`` on Linux. What
travels over that pipe is the same Pybricks protocol the radio carries, framed
with COBS and wrapped in an envelope that supplies what a byte stream lacks.

Two properties of the firmware shape everything here, and both were measured
rather than assumed:

**Subscribing is what connects.** ``pbio_usb_connection_is_active()`` wants the
port open, DTR asserted *and* an explicit subscription. Until then the hub does
not regard anyone as its host -- so it goes on advertising over Bluetooth and
goes on counting down to switching itself off.

**But reads work without it.** So discovery can open a port, ask a hub its name
and close again without taking the hub, without stopping it advertising and
without resetting its idle timer. That is what lets a listing be a listing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Final, final

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from pb_service.hub import protocol as p
from pb_service.hub.transport import (
    DeviceInfo,
    HubCommandError,
    HubLink,
    NotificationHandler,
    ScanFilter,
)
from pb_service.hub.usb import cobs, wire
from pb_service.hub.usb.port import (
    PortInfo,
    PortLister,
    PortOpener,
    SerialPort,
    list_hub_ports,
    open_hub_port,
)

logger = logging.getLogger(__name__)

__all__ = ["UsbHubTransport", "device_id_for"]

SCHEME: Final = "usb"

REPLY_PATIENCE_SECONDS: Final = 5.0
"""How long any one request waits before the hub is treated as unresponsive."""

PROBE_PATIENCE_SECONDS: Final = 2.0
"""How long discovery spends naming one hub before giving up on it."""

POLL_SECONDS: Final = 1.0
"""How often discovery re-enumerates the serial ports.

A poll, and the repository's rule is to wait for events rather than ask
repeatedly, so this owes an explanation. What cannot be waited on is **device
arrival**: neither the operating system's device notifications (``WM_DEVICE-
CHANGE`` and a WinRT device watcher on Windows, netlink from udev on Linux) are
exposed by any serial library, and writing that watcher is a job for a sibling
``aio-`` library rather than for this file. Enumeration itself is only a
directory read, and a hub already seen is never re-opened, so the cost of a turn
is small. It also runs only while somebody is scanning.
"""

_PENDING_REPLIES: Final = 8
"""Frames the reader may hold for a requester before dropping the oldest news."""


def device_id_for(port: PortInfo) -> str:
    """Return the service-facing id for a port.

    Args:
        port: What enumeration reported.

    Returns:
        The prefixed identifier, built from the hub's USB serial number when it
        has one. That is stable across replugging and across a port being
        renumbered, which neither ``COM5`` nor ``/dev/ttyACM0`` is. It is not
        the durable hub identity this repository wants -- it says nothing over
        Bluetooth -- but it is the best a route can offer.
    """
    return f"{SCHEME}:{port.serial_number or port.device}"


@final
class _UsbLink(HubLink):
    """An open connection to one hub over its serial port.

    Requests are serialised, one at a time. That is not caution: a hub answers
    one read at a time, and issuing several without waiting loses all but the
    last -- measured, not documented anywhere. With at most one request
    outstanding, correlating a reply needs no bookkeeping beyond checking that
    it is the one that was asked for.
    """

    def __init__(self, serial: SerialPort, device_id: str) -> None:
        self._serial = serial
        self._device_id = device_id
        self._lock = anyio.Lock()
        self._handler: NotificationHandler | None = None
        self._connected = True
        self._tag = 0

        send, receive = anyio.create_memory_object_stream[cobs.Frame](_PENDING_REPLIES)
        self._replies_in: MemoryObjectSendStream[cobs.Frame] = send
        self._replies_out: MemoryObjectReceiveStream[cobs.Frame] = receive

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def read_characteristic(self, uuid: str) -> bytes:
        selector = wire.READ_SELECTORS.get(uuid)

        if selector is None:
            raise ValueError(f"no USB equivalent for characteristic {uuid}")

        async with self._lock:
            self._discard_stale()
            await self._write(wire.encode_read(selector))

            with anyio.fail_after(REPLY_PATIENCE_SECONDS):
                while True:
                    frame = await self._next_reply()

                    if frame.type != wire.InMessage.READ_REPLY:
                        continue

                    reply = wire.parse_read_reply(frame.payload)

                    if reply is not None and reply[0] == selector:
                        return reply[1]

    async def write_characteristic(
        self, uuid: str, data: bytes, *, response: bool = True
    ) -> None:
        if uuid != p.PYBRICKS_COMMAND_EVENT_UUID:
            raise ValueError(f"characteristic {uuid} is not writable over USB")

        self._tag = (self._tag + 1) % 256
        tag = self._tag

        async with self._lock:
            self._discard_stale()
            await self._write(wire.encode_command(tag, data))

            # The hub answers every command, so the answer is collected even
            # when the caller does not want one. Leaving it in the stream would
            # make it the next request's problem.
            with anyio.fail_after(REPLY_PATIENCE_SECONDS):
                status = await self._await_response(tag)

        if status and response:
            raise _rejection(status)

    async def start_notify(self, uuid: str, handler: NotificationHandler) -> None:
        if uuid != p.PYBRICKS_COMMAND_EVENT_UUID:
            raise ValueError(f"characteristic {uuid} does not notify over USB")

        self._handler = handler

        async with self._lock:
            await self._write(wire.encode_subscribe(subscribed=True))

    async def _await_response(self, tag: int) -> int:
        """Wait for the response carrying ``tag``.

        Args:
            tag: What was sent with the command.

        Returns:
            The firmware's status code, zero for success.
        """
        while True:
            frame = await self._next_reply()

            if frame.type != wire.InMessage.RESPONSE:
                continue

            answer = wire.parse_response(frame.payload)

            if answer is not None and answer.tag == tag:
                return answer.status

    async def _next_reply(self) -> cobs.Frame:
        """Take the next frame addressed to whichever request is waiting.

        Returns:
            The frame.

        Raises:
            ConnectionError: If the link ended while waiting. The stream closing
                under a request is how a hub going away arrives here, and
                reporting it as a disconnect is what makes it the same failure a
                write reports rather than an anyio internal nobody handles.
        """
        try:
            return await self._replies_out.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError) as exc:
            self._connected = False
            raise ConnectionError(f"the hub on {self._device_id} went away") from exc

    async def _write(self, framed: bytes) -> None:
        """Send one framed message, reporting a dead link as a disconnect."""
        try:
            await self._serial.write(framed)
        except (OSError, ConnectionError) as exc:
            self._connected = False
            raise ConnectionError(f"the hub on {self._device_id} went away") from exc

    def _discard_stale(self) -> None:
        """Drop anything left over from a request that gave up.

        A reply to a request nobody is waiting for any more would otherwise be
        read as the answer to the next one.
        """
        while True:
            try:
                self._replies_out.receive_nowait()
            except (anyio.WouldBlock, anyio.EndOfStream, anyio.ClosedResourceError):
                return

    async def run(self, on_lost: Callable[[str], None] | None) -> None:
        """Read frames for as long as the link lasts.

        Events go straight to the notification handler and everything else to
        whichever request is waiting. This is the only task reading the port, so
        nothing here needs a lock.

        Args:
            on_lost: Called with the hub's id if the link ends *by itself*, and
                deliberately not when this task is cancelled. Disconnecting on
                request unwinds through the same code, and reporting that as a
                hub going away would tell somebody to go and rescue a hub that
                is sitting there perfectly happily.
        """
        try:
            while True:
                body = await self._serial.readuntil(bytes((cobs.DELIMITER,)))
                frame = cobs.decode(body[:-1])

                if frame is None:
                    logger.debug("%s: discarding an undecodable frame", self._device_id)
                    continue

                if frame.type == wire.InMessage.EVENT:
                    if self._handler is not None:
                        self._handler(frame.payload)

                    continue

                try:
                    self._replies_in.send_nowait(frame)
                except anyio.WouldBlock:
                    logger.debug(
                        "%s: no one is waiting for %r", self._device_id, frame.type
                    )
        except (
            OSError,
            ConnectionError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as exc:
            logger.info("%s: the link ended: %s", self._device_id, exc)

            if on_lost is not None:
                on_lost(self._device_id)
        finally:
            self._connected = False
            # Both ends: closing only the sending one leaves the receiving one
            # to be collected instead, which the suite reports as a
            # ResourceWarning, and a port opened and closed many times in a run
            # should accumulate nothing at all.
            self._replies_in.close()
            self._replies_out.close()


def _rejection(status: int) -> HubCommandError:
    """Turn a firmware status code into the hub's own refusal.

    Args:
        status: What the response carried.

    Returns:
        The equivalent error, naming the code where it is one we know.
    """
    try:
        error = p.HubError(status)
    except ValueError:
        return HubCommandError(status, f"hub refused the command (error {status:#04x})")

    return HubCommandError(error, f"hub refused the command: {error.name}")


@final
class UsbHubTransport:
    """Finds hubs on the serial ports and opens connections to them."""

    def __init__(
        self,
        list_ports: PortLister = list_hub_ports,
        open_port: PortOpener = open_hub_port,
    ) -> None:
        """Create a transport.

        Both arguments default to the real serial ports, so nothing that only
        wants hubs has to know they exist. They are arguments because a hub on a
        real cable cannot be asked to refuse a command or to vanish halfway
        through one, and those are the paths worth testing.

        Args:
            list_ports: Where the ports that might be hubs come from.
            open_port: How one of them is opened.
        """
        self.on_disconnect: Callable[[str], None] | None = None
        self._list_ports = list_ports
        self._open_port = open_port
        self._named: dict[str, DeviceInfo] = {}

    @property
    def scheme(self) -> str:
        return SCHEME

    async def discover(
        self, scan_filter: ScanFilter
    ) -> AsyncGenerator[DeviceInfo, None]:
        """Report hubs on the serial ports until the caller stops listening.

        Args:
            scan_filter: Which hubs to report.

        Yields:
            Each hub currently plugged in, re-reported on every turn so that a
            caller sees it as present rather than only once.
        """
        while True:
            for port in self._list_ports():
                device_id = device_id_for(port)

                if device_id in scan_filter.exclude:
                    continue

                info = self._named.get(device_id)

                if info is None:
                    info = await self._probe(port, device_id)

                    if info is None:
                        continue

                    self._named[device_id] = info

                if scan_filter.name is not None and info.name != scan_filter.name:
                    continue

                yield info

            await anyio.sleep(POLL_SECONDS)

    async def _probe(self, port: PortInfo, device_id: str) -> DeviceInfo | None:
        """Ask a hub what it is called, without becoming its host.

        Args:
            port: The port to ask.
            device_id: The id it will be reported under.

        Returns:
            What was learned, or ``None`` if the port could not be opened or did
            not answer -- which is what a port belonging to something other than
            a Pybricks hub looks like, and also what a hub already in use looks
            like.
        """
        # Declared out here because a task group's exit may replace whatever its
        # body raised, so nothing assigned inside one is certain to exist after.
        name: str | None = None
        raw_pnp: bytes | None = None

        try:
            with anyio.fail_after(PROBE_PATIENCE_SECONDS):
                async with self._open_port(port.device) as serial:
                    link = _UsbLink(serial, device_id)

                    async with anyio.create_task_group() as tg:
                        tg.start_soon(link.run, None)

                        name = (
                            await link.read_characteristic(p.DEVICE_NAME_UUID)
                        ).decode("utf-8", "replace")
                        raw_pnp = await link.read_characteristic(p.PNP_ID_UUID)

                        tg.cancel_scope.cancel()
        except (OSError, ConnectionError, TimeoutError, ValueError) as exc:
            logger.debug("%s: not a hub we can talk to: %s", port.device, exc)
            return None

        if name is None or raw_pnp is None:
            return None

        hub_type: str | None = None

        with suppress(ValueError):
            hub_type = p.hub_type_name(p.unpack_pnp_id(raw_pnp))

        return DeviceInfo(
            id=device_id,
            name=name,
            address=port.device,
            rssi=None,
            transport=SCHEME,
            hub_type=hub_type,
        )

    @asynccontextmanager
    async def open(self, device_id: str) -> AsyncGenerator[HubLink, None]:
        """Connect to a hub, subscribing so that the hub knows it has a host.

        Args:
            device_id: An id previously reported by :meth:`discover`.

        Yields:
            The open link, which disconnects on exit.
        """
        port = next(
            (port for port in self._list_ports() if device_id_for(port) == device_id),
            None,
        )

        if port is None:
            raise ConnectionError(f"no hub is plugged in as {device_id}")

        async with self._open_port(port.device) as serial:
            link = _UsbLink(serial, device_id)

            async with anyio.create_task_group() as tg:
                tg.start_soon(link.run, self._on_lost)

                try:
                    yield link
                finally:
                    # Unsubscribing hands the hub back: it stops regarding us as
                    # its host, so it advertises again and can be reached over
                    # Bluetooth. Shielded because it is worth doing even when
                    # the reason we are leaving is a cancellation.
                    with (
                        anyio.CancelScope(shield=True),
                        anyio.move_on_after(1.0),
                        suppress(OSError, ConnectionError),
                    ):
                        await serial.write(wire.encode_subscribe(subscribed=False))

                    tg.cancel_scope.cancel()

    def _on_lost(self, device_id: str) -> None:
        """Report a hub that went away by itself."""
        self._named.pop(device_id, None)

        if self.on_disconnect is not None:
            self.on_disconnect(device_id)
