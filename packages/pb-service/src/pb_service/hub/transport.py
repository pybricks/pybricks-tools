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
from typing import Final, Protocol, runtime_checkable

from pb_service.hub import protocol as p

__all__ = [
    "RECONNECTING_TRANSPORTS",
    "TRANSPORT_PREFERENCE",
    "DeviceInfo",
    "HubCommandError",
    "HubLink",
    "HubTransport",
    "NotificationHandler",
    "ScanFilter",
    "UnavailableDevice",
    "looks_like_id",
    "preference_rank",
    "reconnects",
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
class UnavailableDevice:
    """Something a transport can see but was not allowed to use.

    A scan that finds nothing is ambiguous: there may be no hub, or there may
    be one the computer will not let us near. Only the transport can tell the
    two apart, and it cannot say so by raising -- one transport's problem must
    not stop the others from answering -- so it says so by remembering.
    """

    address: str
    """What could not be opened, such as ``/dev/ttyACM0`` or ``COM5``."""

    reason: str
    """The operating system's own words, kept rather than paraphrased.

    They are the half of the message that says which of several quite different
    problems this is, and the client turns the code into the advice.
    """


TRANSPORT_PREFERENCE: Final = ("usb", "ble", "fake")
"""How transports rank when a hub is reachable more than one way, best first.

USB comes first on three counts, each measured rather than argued: enumerating
serial ports answers in milliseconds where a Bluetooth scan has to wait to hear
something; a bulk endpoint has no connection interval to wait for; and a hub
running a program does not advertise, so USB can reach one that a Bluetooth scan
cannot see at all.

The order here is the one :func:`~pb_service.backend.get_hub_transport` builds
its transports in, and a test holds the two together.
"""


def preference_rank(transport: str) -> int:
    """Rank a transport, lower being preferred.

    Args:
        transport: A transport's scheme.

    Returns:
        Its position in :data:`TRANSPORT_PREFERENCE`, or a rank worse than any
        of them for one this build has never heard of. An unknown transport
        losing every tie is the safe way round: it can still be used, just not
        chosen over something known.
    """
    try:
        return TRANSPORT_PREFERENCE.index(transport)
    except ValueError:
        return len(TRANSPORT_PREFERENCE)


RECONNECTING_TRANSPORTS: Final = frozenset({"usb"})
"""Transports where losing a hub is worth waiting out rather than just reporting.

A cable is pulled out by a person and put back by one, so a hub that reappears
on the same port is the same hub being handed back to the same work -- which is
why the service goes back to it without being asked. A radio is not like that.
A hub coming into range again says nothing about whether anybody wants it, and
connecting would stop it advertising to whoever actually does; a hub switched
off and on is quite likely being handed to somebody else. The difference is
about what the reappearance means rather than anything about the two protocols,
so it is recorded here as a decision rather than derived from a capability.
"""


def reconnects(transport: str) -> bool:
    """Say whether a hub lost on this transport should be waited for.

    Args:
        transport: A transport's scheme.

    Returns:
        True if the hub coming back is worth acting on. False for anything this
        build has not thought about, which is the safe way round: not going back
        to a hub is an inconvenience, whereas taking one that was not offered is
        a hub quietly removed from whoever else was reaching for it.
    """
    return transport in RECONNECTING_TRANSPORTS


@dataclass(frozen=True, slots=True)
class ScanFilter:
    """Which hubs a scan should report."""

    name: str | None = None
    """Match this advertised name, or any name when ``None``."""

    exclude: frozenset[str] = field(default_factory=frozenset[str])
    """Ids to skip, typically ones already connected."""

    transport: str | None = None
    """Only look on this transport, or on every one when ``None``.

    Carried in the filter rather than applied to the results so that asking for
    USB does not start the radio: a transport that cannot match is never
    started at all.
    """


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

    @property
    def unavailable(self) -> tuple[UnavailableDevice, ...]:
        """What the last scan could see but could not open.

        Empty for a transport that has nothing to report, which is the usual
        answer and the only one some transports can give. It is read after a
        scan rather than during one, because it answers a question that only
        arises once the scan has come up empty: a hub that was found needs no
        explaining.

        Whatever is here is from the most recent scan. A port that has since
        been unplugged, or that opened on a later pass, is gone from it.
        """
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
