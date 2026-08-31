# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Bluetooth Low Energy transport, built on bleak.

bleak is asyncio-native, which is why the whole project runs anyio on its
asyncio backend rather than choosing freely.

The important constraint here is that bleak delivers notifications and
disconnects through callbacks invoked outside our task tree. This module is one
of only two places allowed to receive such callbacks, and it does the least
possible in them: it hands the bytes straight to the connection, which enqueues
them without blocking. Blocking in a bleak callback stalls the event loop, and
with it every other hub.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, final

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakGATTProtocolError

from pb_service.firmware.bootloader import lwp3
from pb_service.hub import protocol as p
from pb_service.hub.transport import (
    DeviceInfo,
    HubCommandError,
    HubLink,
    NotificationHandler,
    ScanFilter,
)

logger = logging.getLogger(__name__)

__all__ = ["BleHubTransport", "device_id_for"]

SCHEME = "ble"


def device_id_for(address: str) -> str:
    """Return the service-facing id for a Bluetooth address.

    Args:
        address: The address bleak reports. A MAC address on Linux and Windows,
            but an opaque per-machine UUID on macOS, so ids are not portable
            between computers.

    Returns:
        The prefixed identifier.
    """
    return f"{SCHEME}:{address.upper()}"


def _advertised_pnp_id(advertisement: AdvertisementData) -> p.PnpId | None:
    """Read the hub's Plug and Play ID out of its scan response.

    The firmware puts the same seven bytes there that the Device Information
    Service serves once connected, as service data under the PnP ID's own
    characteristic UUID. That is what makes it possible to say what kind of hub
    each one is while scanning, without connecting to any of them.

    Args:
        advertisement: What bleak reported, advertisement and scan response
            merged.

    Returns:
        The decoded identifier, or ``None`` if this hub did not advertise one
        or advertised something malformed. Either way the hub is still listed:
        not knowing what kind it is is no reason to hide it.
    """
    data = advertisement.service_data.get(p.PNP_ID_UUID)
    if data is None:
        return None
    try:
        return p.unpack_pnp_id(data)
    except ValueError:
        logger.debug("ignoring a malformed advertised PnP ID: %r", data)
        return None


def _lego_advertisement(advertisement: AdvertisementData) -> lwp3.Advertisement | None:
    """Read a LEGO advertisement, if this is one.

    A hub running LEGO's own firmware, or one sitting in its bootloader, puts
    six bytes under LEGO's company id and advertises a service that says
    which of the two it is. A hub running Pybricks does neither, so this
    returns ``None`` for one and the Pybricks path below handles it.

    Args:
        advertisement: What bleak reported.

    Returns:
        What it says, or ``None``.
    """
    data = advertisement.manufacturer_data.get(lwp3.LEGO_COMPANY_ID)
    if data is None:
        return None
    services = frozenset(uuid.lower() for uuid in advertisement.service_uuids)
    return lwp3.read_advertisement(bytes(data), services)


def _described(found: lwp3.Advertisement) -> str:
    """Name a device that has no name of its own to give.

    Args:
        found: What its advertisement said.

    Returns:
        Something to show a user, since a blank row would be worse.
    """
    return "LEGO bootloader" if found.in_bootloader else "LEGO firmware"


def _services_for(scan_filter: ScanFilter) -> list[str]:
    """Return the service UUIDs a scan should look for.

    Asked for by kind rather than always looking for all three: a scan filtered
    at the radio is a scan that does not wake this process for every unrelated
    device in the building, and the ordinary commands only ever want Pybricks
    hubs.

    Args:
        scan_filter: What the caller is looking for.

    Returns:
        The UUIDs to give the scanner.
    """
    wanted: list[str] = []
    if "pybricks" in scan_filter.kinds:
        wanted.append(p.PYBRICKS_SERVICE_UUID)
    if "lego" in scan_filter.kinds:
        wanted.append(lwp3.HUB_SERVICE_UUID)
    if "bootloader" in scan_filter.kinds:
        wanted.append(lwp3.BOOTLOADER_SERVICE_UUID)
    return wanted


def _rejection(exc: BleakGATTProtocolError) -> HubCommandError:
    """Turn a GATT protocol error into the hub's own refusal.

    Args:
        exc: What bleak raised.

    Returns:
        The equivalent :class:`HubCommandError`, naming the firmware error
        where it is one we know.
    """
    code = int(exc.args[0])
    try:
        error = p.HubError(code)
    except ValueError:
        return HubCommandError(code, f"hub refused the command (error {code:#04x})")
    return HubCommandError(error, f"hub refused the command: {error.name}")


@final
class _BleLink(HubLink):
    """An open GATT connection to one hub."""

    def __init__(self, client: BleakClient) -> None:
        self._client = client

    async def read_characteristic(self, uuid: str) -> bytes:
        return bytes(await self._client.read_gatt_char(uuid))

    async def write_characteristic(
        self, uuid: str, data: bytes, *, response: bool = True
    ) -> None:
        try:
            await self._client.write_gatt_char(uuid, data, response=response)
        except BleakGATTProtocolError as exc:
            # A GATT application error is how the firmware says no: the code
            # is a pbio_pybricks_error_t, and losing it here would turn every
            # refusal into an unexplained failure.
            raise _rejection(exc) from exc

    async def start_notify(self, uuid: str, handler: NotificationHandler) -> None:
        def on_notify(_sender: Any, data: bytearray) -> None:
            # Called by bleak outside our task tree. Hand the bytes over and
            # return: no awaiting, no task creation, no blocking.
            handler(bytes(data))

        await self._client.start_notify(uuid, on_notify)

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected


@final
class BleHubTransport:
    """Finds and connects to Pybricks hubs over Bluetooth."""

    def __init__(self) -> None:
        """Create the transport."""
        self._devices: dict[str, BLEDevice] = {}
        self.on_disconnect: Callable[[str], None] | None = None
        """Called with a hub id when bleak reports an unexpected disconnect."""

    @property
    def scheme(self) -> str:
        """Identifier prefix for Bluetooth hubs."""
        return SCHEME

    async def discover(self, scan_filter: ScanFilter) -> AsyncIterator[DeviceInfo]:
        """Report Pybricks hubs as their advertisements arrive.

        Filtering on the Pybricks service UUID keeps other Bluetooth devices out
        of the results entirely, rather than showing them and hoping the user
        can tell which is theirs.

        Args:
            scan_filter: Which hubs to report.

        Yields:
            Hubs as they are seen. A hub may be reported more than once as its
            advertisement repeats; callers deduplicate by id.
        """
        async with BleakScanner(service_uuids=_services_for(scan_filter)) as scanner:
            async for device, advertisement in scanner.advertisement_data():
                info = self._to_device_info(device, advertisement)
                if info is None or info.kind not in scan_filter.kinds:
                    continue
                if scan_filter.name is not None and info.name != scan_filter.name:
                    continue
                if info.id in scan_filter.exclude:
                    continue
                self._devices[info.id] = device
                yield info

    def _to_device_info(
        self, device: BLEDevice, advertisement: AdvertisementData
    ) -> DeviceInfo | None:
        lego = _lego_advertisement(advertisement)
        if lego is not None:
            # A hub in its bootloader has no name of its own worth showing --
            # it is not running the firmware that would know one -- so it is
            # described by what it is instead.
            return DeviceInfo(
                id=device_id_for(device.address),
                name=advertisement.local_name or device.name or _described(lego),
                address=device.address,
                rssi=advertisement.rssi,
                transport=SCHEME,
                hub_type=p.hub_type_name(
                    p.PnpId(
                        vendor_id_source=p.VendorIdSource.BLUETOOTH_SIG,
                        vendor_id=p.LEGO_COMPANY_ID,
                        product_id=lego.hub_type,
                        product_version=0,
                    )
                ),
                kind="bootloader" if lego.in_bootloader else "lego",
            )

        # The name arrives in the scan response, which may lag the advertisement,
        # so a hub without one yet is skipped rather than shown as unnamed.
        name = advertisement.local_name or device.name
        if not name:
            return None
        return DeviceInfo(
            id=device_id_for(device.address),
            name=name,
            address=device.address,
            rssi=advertisement.rssi,
            transport=SCHEME,
            hub_type=p.hub_type_name(_advertised_pnp_id(advertisement)),
        )

    def open(self, device_id: str) -> AbstractAsyncContextManager[HubLink]:
        """Connect to a hub.

        Args:
            device_id: An id from :meth:`discover`.

        Returns:
            An async context manager yielding the link.
        """
        return self._open(device_id)

    @asynccontextmanager
    async def _open(self, device_id: str) -> AsyncGenerator[HubLink, None]:
        device = self._devices.get(device_id)
        if device is None:
            raise ConnectionRefusedError(
                f"{device_id} has not been seen; scan for it first"
            )

        def on_disconnect(_client: BleakClient) -> None:
            # Also outside our task tree, so it only reports the fact.
            logger.info("bluetooth reported a disconnect for %s", device_id)
            if self.on_disconnect is not None:
                self.on_disconnect(device_id)

        client = BleakClient(device, disconnected_callback=on_disconnect)
        logger.debug("connecting over bluetooth to %s", device_id)
        async with client:
            yield _BleLink(client)
