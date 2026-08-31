# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The LEGO bootloader that Move, City and Technic hubs run.

The wire format only: what to send and how to read what comes back. Doing the
sending is :mod:`pb_service.firmware.bootloader.powered_up`, which keeps this
half pure and therefore testable without a hub, a radio or a clock.

This is also where a hub is recognised before anything connects to it. A hub
can be found in three states -- running Pybricks, running LEGO's own firmware,
or already sitting in its bootloader -- and each needs different treatment, so
telling them apart from the advertisement alone is what lets the tool say what
it found instead of guessing.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

__all__ = [
    "BOOTLOADER_CHARACTERISTIC_UUID",
    "BOOTLOADER_SERVICE_UUID",
    "HUB_CHARACTERISTIC_UUID",
    "HUB_SERVICE_UUID",
    "LEGO_COMPANY_ID",
    "PAYLOAD_SIZES",
    "Advertisement",
    "BootloaderCommand",
    "BootloaderInfo",
    "encode_erase",
    "encode_get_checksum",
    "encode_get_info",
    "encode_init_loader",
    "encode_program",
    "encode_reboot_to_bootloader",
    "encode_start_app",
    "read_advertisement",
    "read_info",
    "read_program_reply",
]


def _uuid(short: int) -> str:
    """Expand one of LEGO's 16-bit UUIDs into the full 128-bit form."""
    return f"0000{short:04x}-1212-efde-1623-785feabcd123"


HUB_SERVICE_UUID: Final = _uuid(0x1623)
"""Advertised by a hub running LEGO's own firmware."""

HUB_CHARACTERISTIC_UUID: Final = _uuid(0x1624)
"""Where a hub running LEGO's firmware is told to reboot for an update."""

BOOTLOADER_SERVICE_UUID: Final = _uuid(0x1625)
"""Advertised by a hub sitting in its bootloader."""

BOOTLOADER_CHARACTERISTIC_UUID: Final = _uuid(0x1626)
"""Where the whole flashing conversation happens."""

LEGO_COMPANY_ID: Final = 0x0397
"""LEGO's Bluetooth company identifier, which its advertisements are keyed by."""

_ADVERTISEMENT_SIZE: Final = 6
"""Both kinds of LEGO advertisement carry exactly this much."""


class BootloaderCommand(IntEnum):
    """What can be sent to the bootloader characteristic."""

    ERASE_FLASH = 0x11
    PROGRAM_FLASH = 0x22
    START_APP = 0x33
    """Runs the firmware, which disconnects Bluetooth -- so there is no reply."""

    INIT_LOADER = 0x44
    GET_INFO = 0x55
    GET_CHECKSUM = 0x66
    GET_FLASH_STATE = 0x77
    DISCONNECT = 0x88


PAYLOAD_SIZES: Final[Mapping[int, int]] = {
    0x40: 14,
    0x41: 32,
    0x80: 32,
}
"""How much firmware fits in one write, by hub type.

Fourteen on the Move hub because its Bluetooth maximum transmission unit is
23 bytes and cannot be negotiated up. Getting this wrong does not fail
cleanly: the write is refused or truncated part way through a flash.
"""

_FIRMWARE_UPDATE_MESSAGE: Final = bytes([12, 0x00, 0x10]) + b"LPF2-Boot"
"""What tells a hub running LEGO's firmware to reboot into its bootloader.

A length, a hub id of zero, the message kind, and an ASCII safety key -- which
is presumably there so that this cannot happen by accident.
"""


@dataclass(frozen=True, slots=True)
class Advertisement:
    """What a LEGO advertisement says about the hub sending it."""

    hub_type: int
    """The ``device-id``, the same number a firmware archive is keyed by."""

    in_bootloader: bool
    """Whether it is sitting in its bootloader rather than running firmware."""

    version: int = 0
    """The bootloader's version, where it is one. Zero otherwise."""

    button_pressed: bool = False
    """Whether the hub's button is down, where it says. Never in a bootloader."""


@dataclass(frozen=True, slots=True)
class BootloaderInfo:
    """What the bootloader says when asked about itself."""

    version: int
    start_address: int
    """Where firmware is written, and where a flash begins."""

    end_address: int
    hub_type: int
    """Which hub this really is, which is the thing worth checking.

    The advertisement said so too, but this comes from the device that is
    about to be written to, so it is the one to believe.
    """

    @property
    def payload_size(self) -> int:
        """How much firmware to put in one write.

        Returns:
            The size for this hub, defaulting to the smallest anything uses --
            a hub nobody has heard of is better flashed slowly than refused,
            and better slowly than in writes it cannot take.
        """
        return PAYLOAD_SIZES.get(self.hub_type, min(PAYLOAD_SIZES.values()))


def read_advertisement(
    manufacturer_data: bytes, service_uuids: frozenset[str]
) -> Advertisement | None:
    """Read what a LEGO advertisement says, if it is one.

    Args:
        manufacturer_data: The bytes advertised under LEGO's company id.
        service_uuids: The service UUIDs in the advertisement, lowercased.

    Returns:
        What it says, or ``None`` if this is not a LEGO hub advertisement --
        which includes a hub running Pybricks, since that advertises its own
        service and its Plug and Play id instead.
    """
    if len(manufacturer_data) != _ADVERTISEMENT_SIZE:
        return None

    if BOOTLOADER_SERVICE_UUID in service_uuids:
        # Four bytes of version, then the hub type. Nothing about a button:
        # there is no firmware running to have noticed one.
        return Advertisement(
            hub_type=manufacturer_data[4],
            in_bootloader=True,
            version=int.from_bytes(manufacturer_data[:4], "little"),
        )

    if HUB_SERVICE_UUID in service_uuids:
        return Advertisement(
            hub_type=manufacturer_data[1],
            in_bootloader=False,
            button_pressed=bool(manufacturer_data[0]),
        )

    return None


def encode_reboot_to_bootloader() -> bytes:
    """Build the message that reboots a hub running LEGO's own firmware.

    Returns:
        The bytes to write to the hub characteristic. There is no reply and
        never will be: the hub reboots, so the connection drops instead.
    """
    return _FIRMWARE_UPDATE_MESSAGE


def encode_get_info() -> bytes:
    """Build a request for the bootloader's own details."""
    return bytes([BootloaderCommand.GET_INFO])


def read_info(reply: bytes) -> BootloaderInfo:
    """Read what the bootloader answered about itself.

    Args:
        reply: The notification, starting with the command it answers.

    Returns:
        What it said.

    Raises:
        ValueError: If this is not an answer to that question, or is too short
            to be one.
    """
    expected = struct.calcsize("<iIIB") + 1
    if len(reply) < expected or reply[0] != BootloaderCommand.GET_INFO:
        raise ValueError(f"not a bootloader info reply: {reply.hex(' ')}")
    version, start, end, hub_type = struct.unpack("<iIIB", reply[1:expected])
    return BootloaderInfo(
        version=version, start_address=start, end_address=end, hub_type=hub_type
    )


def encode_erase() -> bytes:
    """Build a request to erase the firmware that is there now."""
    return bytes([BootloaderCommand.ERASE_FLASH])


def encode_init_loader(size: int) -> bytes:
    """Build a request to get ready for a firmware of a given size.

    Args:
        size: How many bytes are about to be written.

    Returns:
        The request.
    """
    return bytes([BootloaderCommand.INIT_LOADER]) + struct.pack("<I", size)


def encode_program(address: int, payload: bytes) -> bytes:
    """Build one write of firmware.

    Args:
        address: Where in flash this piece goes.
        payload: The piece, no longer than the hub's payload size.

    Returns:
        The request: the command, then a length that counts the address as
        well as the data, then the address, then the data.
    """
    return (
        bytes([BootloaderCommand.PROGRAM_FLASH, len(payload) + 4])
        + struct.pack("<I", address)
        + payload
    )


def read_program_reply(reply: bytes) -> tuple[int, int]:
    """Read the answer to the last write of a flash.

    Only the last one is answered; the rest are written without a reply,
    because waiting for one per chunk would make flashing take all day.

    Args:
        reply: The notification.

    Returns:
        The checksum the hub computed and how many bytes it took.

    Raises:
        ValueError: If this is not that answer.
    """
    expected = struct.calcsize("<BI") + 1
    if len(reply) < expected or reply[0] != BootloaderCommand.PROGRAM_FLASH:
        raise ValueError(f"not a flash reply: {reply.hex(' ')}")
    checksum, count = struct.unpack("<BI", reply[1:expected])
    return checksum, count


def encode_get_checksum() -> bytes:
    """Build a request for the running checksum.

    Sent every so often during a flash for a reason that has nothing to do
    with checksums: writes are otherwise unacknowledged, so this is the only
    way to find out whether the hub is keeping up. It throttles the sender to
    a rate the hub can take.
    """
    return bytes([BootloaderCommand.GET_CHECKSUM])


def encode_start_app() -> bytes:
    """Build a request to run the firmware that was just written.

    Returns:
        The request. Like the reboot above there is no reply, because the hub
        restarts and Bluetooth goes with it.
    """
    return bytes([BootloaderCommand.START_APP])
