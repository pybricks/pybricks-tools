# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Where a serial port comes from, and the little of one this needs.

Two functions and a protocol, kept apart from the transport for one reason: a
hub on the end of a real cable will not misbehave on request. The link's own
behaviour -- a command the hub refuses, a reply that never arrives, a hub that
goes away mid-request, a frame that will not decode -- is only reachable with a
port that can be told to do those things, so the transport takes its ports from
whoever built it rather than reaching for `serialx` itself.

What is asked of a port is deliberately two methods. Everything else about
`serialx.AsyncSerial` -- the baud rate a CDC device ignores, the modem pins, the
flow control -- belongs to opening one, and so lives in :func:`open_hub_port`
rather than in the protocol.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final, NamedTuple

import serialx

from pb_service.hub.stream.pipe import BytePipe

logger = logging.getLogger(__name__)

__all__ = [
    "LEGO_VENDOR_ID",
    "PortInfo",
    "PortLister",
    "PortOpener",
    "list_hub_ports",
    "open_hub_port",
]

LEGO_VENDOR_ID: Final = 0x0694
"""The USB vendor id every LEGO hub enumerates with."""

BAUD_RATE: Final = 115200
"""Meaningless to a CDC device, which ignores it, but the API wants a number."""


class PortInfo(NamedTuple):
    """A serial port that might be a hub.

    The three fields the transport actually reads, rather than the library's own
    record, so that a test can describe a port without building one.
    """

    device: str
    """What to open: ``COM5``, or ``/dev/ttyACM0``."""

    serial_number: str | None = None
    """The hub's USB serial number, where the descriptor carries one.

    This is what a hub is identified by, because it survives replugging and a
    port being renumbered, which the device name does not.
    """

    product: str | None = None
    """What the device calls itself, where the operating system reports it.

    Pybricks firmware appends its own mark -- an Inventor hub reports ``LEGO
    Technic Large Hub + Pybricks``. Not filtered on, because Windows reports the
    serial port's description here rather than the device's, so requiring the
    mark would hide every hub on the primary development platform.
    """


PortLister = Callable[[], Iterable[PortInfo]]
"""Where the list of ports that might be hubs comes from."""

PortOpener = Callable[[str], AbstractAsyncContextManager[BytePipe]]
"""How a port is opened, given its device name."""


def list_hub_ports() -> Iterable[PortInfo]:
    """Enumerate the serial ports that might be hubs.

    Returns:
        Every port enumerating with LEGO's vendor id.
    """
    return (
        PortInfo(
            device=port.device,
            serial_number=port.serial_number,
            product=port.product,
        )
        for port in serialx.list_serial_ports()
        if port.vid == LEGO_VENDOR_ID
    )


@asynccontextmanager
async def open_hub_port(device: str) -> AsyncGenerator[BytePipe, None]:
    """Open a serial port ready to talk to a hub.

    Asserting DTR is part of opening rather than something the caller does,
    because it is not a setting: it is how the firmware knows a host is there at
    all, and how it notices one that goes away. ``pbio_usb_dtr`` is one of the
    three terms of ``pbio_usb_connection_is_active()``, and the firmware's own
    comment records that hosts rarely unsubscribe explicitly and that letting
    DTR drop is what it relies on -- so it is asserted here and dropped by the
    close, and neither is optional.

    Args:
        device: The port to open.

    Yields:
        The open port.
    """
    async with serialx.AsyncSerial(device, baudrate=BAUD_RATE) as serial:
        await serial.set_modem_pins(dtr=serialx.PinState.HIGH)
        yield serial
