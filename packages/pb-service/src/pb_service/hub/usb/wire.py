# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The messages a hub exchanges over USB.

The commands themselves are the ones the Bluetooth transport already sends --
:mod:`pb_service.hub.protocol` builds them and nothing here duplicates that.
What this module adds is the envelope a byte stream needs and a radio does not:
a message type, a tag so a response can be matched to the command that caused
it, and a way to name a characteristic now that there is no GATT to do it.

Three things about that envelope are easy to get wrong and are pinned by tests:

* **A read has no tag.** Only commands do. A read reply is matched by the
  ``[service, characteristic]`` selector the hub echoes back.
* **Reads cannot be issued back to back.** A hub answers one at a time, and
  sending several without waiting loses all but the last.
* **Events arrive between anything and anything else**, so a reply is never
  simply the next frame.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from enum import IntEnum
from typing import Final, NamedTuple

from pb_service.hub import protocol as p
from pb_service.hub.usb import cobs

__all__ = [
    "MIN_PROFILE",
    "READ_SELECTORS",
    "InMessage",
    "OutMessage",
    "ReadService",
    "Response",
    "Selector",
    "encode_command",
    "encode_read",
    "encode_subscribe",
    "parse_read_reply",
    "parse_response",
]

MIN_PROFILE: Final[p.Version] = (1, 6, 0)
"""Oldest profile with a USB interface at all.

Four minor versions above the Bluetooth floor, so a hub too old for this is one
to reach over Bluetooth rather than one to report an error about.
"""


class OutMessage(IntEnum):
    """Host to hub message types."""

    SUBSCRIBE = 1
    COMMAND = 2
    READ = 3


class InMessage(IntEnum):
    """Hub to host message types."""

    RESPONSE = 1
    EVENT = 2
    READ_REPLY = 3


class ReadService(IntEnum):
    """Which family a characteristic id belongs to."""

    GATT = 0x01
    PYBRICKS = 0x02


class Selector(NamedTuple):
    """Names a characteristic, standing in for a GATT UUID."""

    service: ReadService
    characteristic: int


READ_SELECTORS: Final[Mapping[str, Selector]] = {
    p.DEVICE_NAME_UUID: Selector(ReadService.GATT, 0x2A00),
    p.FIRMWARE_REVISION_UUID: Selector(ReadService.GATT, 0x2A26),
    p.SOFTWARE_REVISION_UUID: Selector(ReadService.GATT, 0x2A28),
    p.PNP_ID_UUID: Selector(ReadService.GATT, 0x2A50),
    p.PYBRICKS_HUB_CAPABILITIES_UUID: Selector(ReadService.PYBRICKS, 0x0003),
}
"""Every characteristic the handshake reads, by the UUID it is known by above.

The USB interface deliberately mirrors the Bluetooth one, so this is a
translation rather than a second protocol: the same five values come back, and
:class:`~pb_service.hub.connection.HubConnection` cannot tell which transport
answered.
"""


class Response(NamedTuple):
    """A hub's answer to one command."""

    tag: int
    """Echoed from the command, so a late response is still attributable."""

    status: int
    """The firmware's own error code. Zero is success."""


def encode_subscribe(*, subscribed: bool) -> bytes:
    """Frame a subscription request.

    Subscribing is what makes the hub send events at all, and it is also half of
    what makes the hub regard a host as connected -- so it is what stops the hub
    switching itself off, and what stops it advertising over Bluetooth.

    Args:
        subscribed: Whether to receive events.

    Returns:
        The framed message.
    """
    return cobs.encode(OutMessage.SUBSCRIBE, bytes((1 if subscribed else 0,)))


def encode_read(selector: Selector) -> bytes:
    """Frame a characteristic read request.

    Args:
        selector: What to read.

    Returns:
        The framed message.
    """
    return cobs.encode(
        OutMessage.READ,
        struct.pack("<BH", selector.service, selector.characteristic),
    )


def encode_command(tag: int, command: bytes) -> bytes:
    """Frame a Pybricks command.

    Args:
        tag: A byte chosen by the host, echoed in the response. It exists
            because a byte stream has no transaction correlation of its own,
            where a Bluetooth write with response did.
        command: The command bytes, encoded exactly as for a Bluetooth write.

    Returns:
        The framed message.
    """
    return cobs.encode(OutMessage.COMMAND, bytes((tag,)) + command)


def parse_response(payload: bytes) -> Response | None:
    """Read a command response.

    Args:
        payload: The decoded frame payload.

    Returns:
        The response, or ``None`` if it was too short to be one.
    """
    if len(payload) < 5:
        return None

    tag, status = struct.unpack("<BI", payload[:5])

    return Response(tag, status)


def parse_read_reply(payload: bytes) -> tuple[Selector, bytes] | None:
    """Read a characteristic reply.

    Args:
        payload: The decoded frame payload.

    Returns:
        What was read and its value, or ``None`` if the reply was malformed or
        named a service this build does not know.
    """
    if len(payload) < 3:
        return None

    service, characteristic = struct.unpack("<BH", payload[:3])

    try:
        return Selector(ReadService(service), characteristic), payload[3:]
    except ValueError:
        return None
