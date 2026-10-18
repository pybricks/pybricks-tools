# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""RFCOMM transport: a hub reached over Bluetooth Classic serial.

What travels over the channel is the same framed protocol USB carries, so
everything above :mod:`pb_service.hub.stream` is shared and only getting the
bytes differs.

Discovery is not a scan and cannot be one. Bluetooth Classic pairs in the
operating system, and what is offered here is the paired set -- so a hub shows
up as soon as it is known to the computer and stays whether it is switched on
or not. A listing therefore means "hubs this computer has been introduced to",
not "hubs that are here", and only opening one says which.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Final, final
from uuid import UUID

import aio_rfcomm
import anyio
from aio_rfcomm.errors import BluetoothUnavailableError, RfcommError

from pb_service.hub import protocol as p
from pb_service.hub.rfcomm.port import (
    HUB_SERVICES,
    SERIAL_PORT_SERVICE,
    list_hub_devices,
    open_hub_channel,
    split_hub_name,
)
from pb_service.hub.stream.link import StreamLink
from pb_service.hub.transport import (
    DeviceInfo,
    HubLink,
    ScanFilter,
    UnavailableDevice,
)

logger = logging.getLogger(__name__)

__all__ = ["RfcommHubTransport", "device_id_for"]

SCHEME: Final = "rfcomm"

POLL_SECONDS: Final = 2.0
"""How often the paired set is re-read while someone is listening.

Slower than the USB poll because the answer almost never changes: pairing is
something a person does in system settings, not something that happens while a
scan is running. It is here at all so that a hub paired mid-scan still appears.
"""


def device_id_for(address: str) -> str:
    """Return the service-facing id for a Bluetooth address.

    Args:
        address: The hub's Bluetooth address.

    Returns:
        The prefixed identifier.
    """
    return f"{SCHEME}:{address}"


@final
class RfcommHubTransport:
    """Finds paired hubs and opens RFCOMM channels to them."""

    def __init__(self, bluetooth: aio_rfcomm.Bluetooth) -> None:
        """Create a transport.

        Args:
            bluetooth: Bluetooth, open for at least as long as this is used.
                :func:`~pb_service.backend.open_hub_transport` opens it.
        """
        self._bluetooth = bluetooth
        self.on_disconnect: Callable[[str], None] | None = None
        self._unavailable: dict[str, str] = {}
        # The device as listed and the service it answered on, so that opening
        # it uses the one that found it rather than guessing.
        self._found: dict[str, tuple[aio_rfcomm.RfcommDeviceInfo, UUID]] = {}

    @property
    def scheme(self) -> str:
        return SCHEME

    @property
    def unavailable(self) -> tuple[UnavailableDevice, ...]:
        """What the radio would not let us look at.

        A refused permission or a switched-off adapter, rather than a hub: the
        paired set cannot be read at all in either case, so the one entry here
        stands for the adapter.
        """
        return tuple(
            UnavailableDevice(address, reason, kind="adapter")
            for address, reason in self._unavailable.items()
        )

    async def discover(
        self, scan_filter: ScanFilter
    ) -> AsyncGenerator[DeviceInfo, None]:
        """Report paired hubs until the caller stops listening.

        Args:
            scan_filter: Which hubs to report.

        Yields:
            Each paired hub offering the service, re-reported on every pass.
        """
        if scan_filter.transport is not None and scan_filter.transport != SCHEME:
            return

        # Followed as well as polled, so that Bluetooth being switched off or
        # on is reported at once rather than on the next pass. Only a wake-up:
        # each pass still learns what is wrong from reading the paired set,
        # whose error carries what the platform said, such as how to allow
        # this program.
        changes = self._bluetooth.watch_availability()
        # The state now, which the first pass is about to read anyway.
        await anext(changes)

        while True:
            self._unavailable.clear()

            try:
                seen: set[str] = set()

                for service in HUB_SERVICES:
                    for device in await list_hub_devices(self._bluetooth, service):
                        if device.address in seen:
                            continue

                        bare, kind = split_hub_name(device.name)

                        # Plain Serial Port is not ours: every paired
                        # headset offers it too, so only a name that
                        # identifies a brick makes one of those a hub.
                        if kind is None and service == SERIAL_PORT_SERVICE:
                            continue

                        seen.add(device.address)
                        device_id = device_id_for(device.address)
                        self._found[device_id] = (device, service)

                        if device_id in scan_filter.exclude:
                            continue

                        # Without the type suffix: the listing has its
                        # own column for that.
                        name = bare or device.address

                        if scan_filter.name is not None and name != scan_filter.name:
                            continue

                        yield DeviceInfo(
                            id=device_id,
                            name=name,
                            address=device.address,
                            transport=SCHEME,
                            hub_type=(p.hub_kind_name(kind) if kind else None),
                        )
            except BluetoothUnavailableError as exc:
                # The library's own words, kept rather than paraphrased: they
                # are what tells "turn Bluetooth on" from "allow this program".
                self._unavailable["bluetooth"] = str(exc)
                logger.debug("cannot read the paired set: %s", exc)

            with anyio.move_on_after(POLL_SECONDS):
                await anext(changes)

    @asynccontextmanager
    async def open(self, device_id: str) -> AsyncGenerator[HubLink, None]:
        """Open a channel to a hub.

        Args:
            device_id: An id previously reported by :meth:`discover`.

        Yields:
            The open link, which disconnects on exit.
        """
        address = device_id.removeprefix(f"{SCHEME}:")

        if address == device_id:
            raise ConnectionError(f"{device_id} is not an RFCOMM hub")

        found = self._found.get(device_id)

        if found is None:
            raise ConnectionError(
                f"{device_id} has not been discovered, so there is no knowing "
                f"which service to open"
            )

        device, service = found

        # An exit stack rather than "async with", so that entering is inside
        # the try while the body is not: with an async context manager the
        # work happens in __aenter__.
        async with AsyncExitStack() as stack:
            try:
                pipe = await stack.enter_async_context(
                    open_hub_channel(self._bluetooth, device, service)
                )
            except RfcommError as exc:
                raise ConnectionError(f"cannot reach {device_id}: {exc}") from exc

            link = StreamLink(pipe, device_id)

            async with link.running(self._on_lost):
                yield link

    def _on_lost(self, device_id: str) -> None:
        """Report a hub that went away by itself."""
        if self.on_disconnect is not None:
            self.on_disconnect(device_id)
