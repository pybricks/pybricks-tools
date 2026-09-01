# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The seam between hub logic and the physical link.

Everything above this file speaks the Pybricks protocol in terms of reading and
writing characteristics. Bluetooth is only one way to carry that: the USB
interface deliberately mirrors the same characteristics, and RFCOMM will too, so
adding a transport later should mean adding a file here and changing nothing
above it.

The vocabulary is GATT's because that is the protocol's own vocabulary, not
because Bluetooth is assumed.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pb_service.hub import protocol as p

__all__ = [
    "DeviceInfo",
    "HubCommandError",
    "HubLink",
    "HubTransport",
    "NotificationHandler",
    "ScanFilter",
    "looks_like_id",
]

ID_PREFIXES = ("ble:", "fake:", "usb:", "rfcomm:")
"""How an id announces which transport it belongs to.

Listed here rather than inferred from a colon, because a hub's name is
whatever its owner typed and may perfectly well contain one.
"""


def looks_like_id(value: str) -> bool:
    """Say whether a string is a transport id rather than a hub's name.

    Args:
        value: What a caller supplied to name a hub.

    Returns:
        True if it is an id. Used where the difference matters, which is
        anywhere a value is going to be kept: an id is built from a
        Bluetooth address and stops naming the hub it named today.
    """
    return value.startswith(ID_PREFIXES)


NotificationHandler = Callable[[bytes], None]


class HubCommandError(RuntimeError):
    """The hub refused a command.

    Defined here rather than beside the connection because it belongs to the
    seam: a rejection is something every transport has to report, and the
    simulated hub has to be able to fail in exactly the same way as the radio.
    """

    def __init__(self, error: p.HubError | int, message: str) -> None:
        """Record the firmware's own error code.

        Args:
            error: The raw error value, preserved so callers can be specific
                rather than paraphrasing.
            message: Human-readable description.
        """
        super().__init__(message)
        self.error = error


"""Called when the hub sends a notification.

Synchronous and non-blocking by contract. A transport may invoke this from
outside any task, so it must not await, must not start tasks, and must not
block: on Bluetooth it runs on the event loop thread, and blocking there stalls
the radio for every hub.
"""


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """A hub seen while scanning."""

    id: str
    """Stable identifier, prefixed by transport, for example ``ble:AA:BB:...``.

    Prefixed because the same physical hub can be reachable more than one way,
    and because it leaves room for ``usb:`` and ``rfcomm:`` without a migration.
    """

    name: str
    address: str
    """The transport's own address.

    A Bluetooth address on Linux and Windows, but an opaque per-machine UUID on
    macOS, so it is neither stable across machines nor worth showing to users.
    """

    rssi: int | None = None
    transport: str = "ble"

    hub_type: str | None = None
    """What kind of hub it is, such as ``"City hub"``, if it says.

    Known before connecting because the firmware puts its Plug and Play ID in
    the scan response, so a scan can tell a Prime hub from a Move hub without
    touching either. ``None`` for a hub whose advertisement does not carry one,
    or one this build has no name for.
    """


@dataclass(frozen=True, slots=True)
class ScanFilter:
    """Which hubs a scan should report."""

    name: str | None = None
    """Match this advertised name, or any name when ``None``."""

    exclude: frozenset[str] = field(default_factory=frozenset[str])
    """Ids to skip, typically ones already connected."""


@runtime_checkable
class HubLink(Protocol):
    """An open connection to one hub."""

    async def read_characteristic(self, uuid: str) -> bytes:
        """Read a characteristic value.

        Args:
            uuid: The characteristic to read.

        Returns:
            Its current value.
        """
        ...

    async def write_characteristic(
        self, uuid: str, data: bytes, *, response: bool = True
    ) -> None:
        """Write a characteristic value.

        Args:
            uuid: The characteristic to write.
            data: The bytes to write.
            response: Whether to wait for the hub to acknowledge. Commands that
                can fail need this, because the acknowledgement is what carries
                the error code.
        """
        ...

    async def start_notify(self, uuid: str, handler: NotificationHandler) -> None:
        """Subscribe to notifications from a characteristic.

        Args:
            uuid: The characteristic to subscribe to.
            handler: Called for each notification. See
                :data:`NotificationHandler` for what it may not do.
        """
        ...

    @property
    def is_connected(self) -> bool:
        """Whether the link is still up."""
        ...


@runtime_checkable
class HubTransport(Protocol):
    """Discovers hubs and opens connections to them."""

    on_disconnect: Callable[[str], None] | None
    """Called with a hub's id when the transport loses it.

    The one thing a transport knows that nothing else can find out: a hub that
    is switched off, or carried out of range, says nothing and simply stops
    answering. Without this the service would go on believing it is connected
    until something tried to write to it, and anyone attached to that hub would
    wait for output that is never coming.

    Set by whoever owns the connections. Invoked from outside any task -- on
    Bluetooth it is a callback from the radio's own thread -- so it must only
    record the fact and return.
    """

    @property
    def scheme(self) -> str:
        """Identifier prefix for hubs on this transport, such as ``ble``."""
        ...

    def discover(self, scan_filter: ScanFilter) -> AsyncGenerator[DeviceInfo, None]:
        """Report hubs as they are seen, until the caller stops listening.

        A stream rather than a list, and with no timeout argument, because a
        scan has no natural end: it runs until the caller has seen enough. That
        also means a caller whose cancel scope fires keeps the hubs found so
        far, which returning a list could not offer.

        Args:
            scan_filter: Which hubs to report.

        Returns:
            An async generator of hubs, which may repeat a hub as its
            advertisement is seen again. A generator rather than any
            async iterator because every caller closes the stream when
            it has seen enough, and only a generator promises
            ``aclose``.
        """
        ...

    def open(self, device_id: str) -> AbstractAsyncContextManager[HubLink]:
        """Connect to a hub.

        Args:
            device_id: An id previously returned by :meth:`scan`.

        Returns:
            An async context manager yielding the open link, which disconnects
            on exit.
        """
        ...
