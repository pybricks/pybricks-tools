# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""A serial port with a simulated hub on the far end.

What this exists for is the half of the USB transport a real hub cannot help
with. A hub on a cable answers correctly, promptly and every time, so the paths
that matter when something goes wrong -- a refused command, a reply that never
comes, a hub that vanishes mid-request, a frame that will not decode -- are
unreachable with hardware. Here they are one method call.

**Replies are built by hand rather than with the encoders under test.** The
framing is shared, because :mod:`~pb_service.hub.stream.cobs` is already checked
against frames captured from a real hub and reusing a verified codec proves
nothing wrong; but the envelope is packed here with :mod:`struct`, so a mistake
in ``parse_response`` or ``parse_read_reply`` shows up as a failure instead of
cancelling out against a matching mistake in an encoder beside it.

It lives here rather than under ``tests`` for the reason
:mod:`~pb_service.hub.fake` does: simulated hardware is where a reader looks for
it, and the tests are imported with ``importlib`` so a module beside them is not
importable by name anyway.
"""

from __future__ import annotations

import math
import struct
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Final, final

import anyio

from pb_service.hub import protocol as p
from pb_service.hub.stream import cobs, wire
from pb_service.hub.stream.pipe import BytePipe
from pb_service.hub.usb.port import PortInfo, PortLister, PortOpener

__all__ = ["CAPABILITIES", "FakeHubPort", "lister", "opener"]

SERIAL_NUMBER: Final = "328738633338"
"""A real hub's, so an id in a failure message looks like one from a log."""

DEVICE: Final = "COM5"

PNP_ID: Final = struct.pack("<BHHH", 0x01, p.LEGO_COMPANY_ID, 0x81, 1)
"""An Inventor hub's: Bluetooth SIG source, LEGO, large Technic hub, variant 1.

Seven bytes, which :func:`~pb_service.hub.protocol.unpack_pnp_id` insists on.
"""

CAPABILITIES: Final = struct.pack("<HIII", 511, 0, 52300, 5)
"""What an Inventor hub reports: max write, flags, max program size, slots."""


@final
class FakeHubPort:
    """A serial port that answers as a Pybricks hub would.

    Every departure from a well-behaved hub is something to be switched on
    deliberately, so a test says what is wrong in one line and the default
    stays the hub that works.
    """

    def __init__(
        self,
        *,
        name: str = "Bumblebee",
        device: str = DEVICE,
        serial_number: str = SERIAL_NUMBER,
        firmware: str = "4.1.0b3",
        profile: str = "1.6.0",
        refuse_with: p.HubError | int | None = None,
        answer_reads: bool = True,
        answer_commands: bool = True,
    ) -> None:
        """Put a hub on the end of the cable.

        Args:
            name: What the hub calls itself.
            device: The port it is plugged into.
            serial_number: Its USB serial number, which is what an id is built
                from, so two hubs in one test need two.
            firmware: Its firmware revision.
            profile: Its Pybricks profile version.
            refuse_with: A status code to answer every command with, or None to
                accept them. A refusal is a *reply*, not a silence.
            answer_reads: Whether reads are answered at all. False is a hub
                that has stopped talking without the port noticing.
            answer_commands: Whether commands are answered at all.
        """
        self.subscribed = False
        """Whether the host has subscribed, which is what makes it the host."""

        self.commands: list[bytes] = []
        """Pybricks command bodies received, without the tag."""

        self.reads: list[wire.Selector] = []
        """Characteristics asked for, in order."""

        self.writes = 0
        """How many times the port was written to, however much was in each."""

        self.asked = anyio.Event()
        """Set when a read arrives, so a test can wait for one to be in flight
        rather than sleeping and hoping."""

        self._name = name
        self._device = device
        self._serial_number = serial_number
        self._refuse_with = refuse_with
        self._answer_reads = answer_reads
        self._answer_commands = answer_commands
        self._gone = False
        self._pending = b""

        self._values: dict[wire.Selector, bytes] = {
            wire.READ_SELECTORS[p.DEVICE_NAME_UUID]: name.encode(),
            wire.READ_SELECTORS[p.FIRMWARE_REVISION_UUID]: firmware.encode(),
            wire.READ_SELECTORS[p.SOFTWARE_REVISION_UUID]: profile.encode(),
            wire.READ_SELECTORS[p.PNP_ID_UUID]: PNP_ID,
            wire.READ_SELECTORS[p.PYBRICKS_HUB_CAPABILITIES_UUID]: CAPABILITIES,
        }

        # Unbounded, so that queueing a reply can never block the hub's side of
        # the conversation and turn a protocol bug into a deadlock.
        self._out, self._in = anyio.create_memory_object_stream[bytes | None](math.inf)

    # ---------------------------------------------------------- what a port is

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        """Hand over the next frame the hub has to send.

        Args:
            separator: Ignored. Every item queued is a whole frame ending in
                the delimiter, so there is never a partial one to scan for.

        Returns:
            One framed message, delimiter included.

        Raises:
            OSError: If the hub has gone, which is what a removed device does
                to a read that was already waiting.
        """
        frame = await self._in.receive()

        if frame is None:
            raise OSError("the device has been removed")

        return frame

    async def write(self, data: bytes | bytearray | memoryview) -> None:
        """Receive bytes from the host and answer them.

        Args:
            data: What the host sent.

        Raises:
            OSError: If the hub has gone.
        """
        if self._gone:
            raise OSError("the device has been removed")

        self.writes += 1
        self._pending += bytes(data)

        # Split on the delimiter rather than assuming one write is one frame,
        # because a byte stream promises nothing of the sort.
        while (end := self._pending.find(cobs.DELIMITER)) >= 0:
            body, self._pending = self._pending[:end], self._pending[end + 1 :]
            self._receive(body)

    # ------------------------------------------------------ what a hub does

    def _receive(self, body: bytes) -> None:
        """Act on one frame from the host.

        Args:
            body: The frame without its delimiter.
        """
        frame = cobs.decode(body)

        if frame is None:
            return

        if frame.type == wire.OutMessage.SUBSCRIBE:
            self.subscribed = bool(frame.payload and frame.payload[0])

        elif frame.type == wire.OutMessage.COMMAND:
            self._on_command(frame.payload)

        elif frame.type == wire.OutMessage.READ:
            self._on_read(frame.payload)

    def _on_command(self, payload: bytes) -> None:
        tag, command = payload[0], payload[1:]
        self.commands.append(command)

        if not self._answer_commands:
            return

        status = int(self._refuse_with) if self._refuse_with is not None else 0
        self.send(wire.InMessage.RESPONSE, struct.pack("<BI", tag, status))

    def _on_read(self, payload: bytes) -> None:
        service, characteristic = struct.unpack("<BH", payload[:3])
        selector = wire.Selector(wire.ReadService(service), characteristic)
        self.reads.append(selector)
        self.asked.set()

        if not self._answer_reads:
            return

        value = self._values.get(selector, b"")
        self.send(
            wire.InMessage.READ_REPLY,
            struct.pack("<BH", service, characteristic) + value,
        )

    # ------------------------------------------------------- what a test does

    def send(self, message: wire.InMessage, payload: bytes) -> None:
        """Queue a frame for the host to read.

        Args:
            message: Which kind.
            payload: Its body.
        """
        self.send_raw(cobs.encode(message, payload))

    def send_raw(self, frame: bytes) -> None:
        """Queue bytes for the host to read, however malformed.

        Args:
            frame: A complete frame, delimiter included.
        """
        self._out.send_nowait(frame)

    def send_event(self, payload: bytes) -> None:
        """Send an event, as a hub does at any moment of its choosing.

        Args:
            payload: The notification body, as the Bluetooth transport would
                have delivered it.
        """
        self.send(wire.InMessage.EVENT, payload)

    def send_status(self, flags: int) -> None:
        """Send a status report event.

        Args:
            flags: The status bits.
        """
        self.send_event(bytes((p.Event.STATUS_REPORT,)) + struct.pack("<I", flags))

    def send_undecodable(self) -> None:
        """Send a frame that will not decode, as a corrupted one would not."""
        # A code word claiming a longer block than the frame contains: the
        # decoder has to discard it and stay aligned for the next delimiter.
        self.send_raw(bytes((0xFF, 0x04, cobs.DELIMITER)))

    def vanish(self) -> None:
        """Unplug the cable.

        A read already waiting fails, and so does anything written afterwards,
        which is what a removed device does rather than simply going quiet.
        """
        self._gone = True
        self._out.send_nowait(None)

    def close(self) -> None:
        """Release the queue, for a port nobody will read again."""
        self._out.close()
        self._in.close()

    @property
    def info(self) -> PortInfo:
        """How this hub looks to enumeration."""
        return PortInfo(
            device=self._device,
            serial_number=self._serial_number,
            product=f"LEGO Technic Large Hub + Pybricks {self._name}",
        )


def opener(*ports: FakeHubPort) -> PortOpener:
    """Build an opener that hands out these hubs by the port they are on.

    Args:
        *ports: The hubs plugged in.

    Returns:
        Something with a real opener's shape, so the transport cannot tell.
    """
    by_device = {port.info.device: port for port in ports}

    @asynccontextmanager
    async def open_port(device: str) -> AsyncGenerator[BytePipe, None]:
        found = by_device.get(device)

        if found is None:
            raise OSError(f"cannot open {device}")

        yield found

    return open_port


def lister(*ports: FakeHubPort) -> PortLister:
    """Build a port lister reporting these hubs.

    Args:
        *ports: The hubs plugged in. Call with none for an empty machine.

    Returns:
        Something with a real lister's shape.
    """
    return lambda: [port.info for port in ports]
