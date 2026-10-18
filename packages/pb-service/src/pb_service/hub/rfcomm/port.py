# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Where an RFCOMM channel to a hub comes from.

Discovery here is not a scan. Bluetooth Classic devices are paired in the
operating system first, and what this can offer is the paired set -- so a hub
appears as soon as it is known and stays there whether it is switched on or
not, which is a different promise from the one a radio scan makes.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Final
from uuid import UUID

import aio_rfcomm
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelClosedError,
    DelimiterNotFoundError,
)

from pb_service.hub import protocol as p
from pb_service.hub.protocol import HubType
from pb_service.hub.stream.pipe import BytePipe

logger = logging.getLogger(__name__)

__all__ = [
    "HUB_SERVICES",
    "HUB_TYPE_SUFFIXES",
    "MAX_FRAME",
    "PYBRICKS_RFCOMM_SERVICE",
    "SERIAL_PORT_SERVICE",
    "ChannelPipe",
    "list_hub_devices",
    "open_hub_channel",
    "split_hub_name",
]

PYBRICKS_RFCOMM_SERVICE: Final = UUID(p.PYBRICKS_RFCOMM_SERVICE_UUID)
"""The EV3's own service class."""

SERIAL_PORT_SERVICE: Final = UUID("00001101-0000-1000-8000-00805f9b34fb")
"""Plain Serial Port, which the NXT publishes because it has nothing else yet.

Specific to neither Pybricks nor LEGO: every paired headset and GPS offers it,
so what it finds are candidates rather than hubs.
"""

HUB_SERVICES: Final = (PYBRICKS_RFCOMM_SERVICE, SERIAL_PORT_SERVICE)
"""The service classes a hub may answer on, most specific first."""

MAX_FRAME: Final = 2**16
"""The longest frame read before giving up on the hub, delimiter included.

The same bound USB reads have: serialx reads through an asyncio.StreamReader
left at its default limit.
"""

HUB_TYPE_SUFFIXES: Final[Mapping[str, HubType]] = {
    "MINDSTORMS EV3": HubType.EV3,
    "MINDSTORMS NXT": HubType.NXT,
}
"""Which brick each name suffix means.

The firmware appends "(<type>)" to the hub name, from PBSYS_CONFIG_HUB_TYPE_STR
in its platform config, so the name carries the type where neither the service
class nor anything else does. These are presentation strings rather than
protocol: a rewording upstream stops NXTs being recognised, while EV3s keep
being found by their own service class.
"""


def split_hub_name(name: str | None) -> tuple[str | None, HubType | None]:
    """Separate a hub's own name from the type the firmware appended to it.

    Args:
        name: What the operating system calls the device.

    Returns:
        The name without the suffix, and the kind it named. The kind is
        ``None`` for anything identifying no hub -- which is every other
        serial device this computer happens to be paired with -- and the name
        is then returned unchanged, since the brackets are then the owner's.
    """
    if not name or not name.endswith(")"):
        return name, None

    # The last bracketed part, because the firmware appends its own: a hub
    # whose user-set name also ends in brackets still answers correctly.
    start = name.rfind("(")

    if start < 0:
        return name, None

    kind = HUB_TYPE_SUFFIXES.get(name[start + 1 : -1].strip())

    if kind is None:
        return name, None

    # The firmware joins the two with exactly one space (name, then
    # " (" PBSYS_CONFIG_HUB_TYPE_STR ")", in pbsys_host_get_hub_display_name),
    # so only that one goes. Any other spaces are the owner's, and the hub
    # reports its name with them, which is what a name filter is matched on.
    bare = name[:start].removesuffix(" ")

    return bare or None, kind


class ChannelPipe:
    """An open RFCOMM channel, as the byte stream the link expects."""

    def __init__(self, channel: aio_rfcomm.RfcommChannel) -> None:
        self._channel = channel

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        """Read up to and including the separator.

        Args:
            separator: The byte sequence to stop after.

        Returns:
            Everything up to and including it.

        Raises:
            ConnectionError: The hub hung up before one arrived, Bluetooth
                stopped being usable, or :data:`MAX_FRAME` bytes arrived
                without one. Reported the way a cable being pulled is, so the
                link above treats a radio going quiet and a plug coming out as
                the same thing.
        """
        try:
            body = await self._channel.receive_until(separator, max_bytes=MAX_FRAME)
        except (ChannelClosedError, BluetoothUnavailableError) as exc:
            raise ConnectionError(f"the channel to the hub ended: {exc}") from exc
        except DelimiterNotFoundError as exc:
            raise ConnectionError(
                f"the hub sent a frame too long to read: {exc}"
            ) from exc

        # The link strips the separator itself.
        return body + separator

    async def write(self, data: bytes | bytearray | memoryview) -> None:
        """Send bytes to the hub.

        Args:
            data: What to send.

        Raises:
            ConnectionError: The channel or Bluetooth stopped working.
        """
        try:
            await self._channel.send(bytes(data))
        except (ChannelClosedError, BluetoothUnavailableError) as exc:
            raise ConnectionError(f"the channel to the hub ended: {exc}") from exc


async def list_hub_devices(
    bluetooth: aio_rfcomm.Bluetooth, service: UUID
) -> list[aio_rfcomm.RfcommDeviceInfo]:
    """List the paired devices offering one service.

    One service at a time rather than all at once, because which one answered
    is what names the hub.

    Args:
        bluetooth: Bluetooth, open.
        service: The service class to match.

    Returns:
        What the OS knows about each one.
    """
    return await bluetooth.list_known_devices(service=service)


@asynccontextmanager
async def open_hub_channel(
    bluetooth: aio_rfcomm.Bluetooth,
    device: aio_rfcomm.RfcommDeviceInfo,
    service: UUID,
) -> AsyncGenerator[BytePipe, None]:
    """Open a hub's serial service.

    Args:
        bluetooth: Bluetooth, open.
        device: The hub, as discovery listed it.
        service: The service class discovery matched it on.

    Yields:
        The open channel, as a byte stream.
    """
    async with bluetooth.open_service(device, service) as channel:
        logger.debug("opened the hub serial service on %s", device.address)
        yield ChannelPipe(channel)
