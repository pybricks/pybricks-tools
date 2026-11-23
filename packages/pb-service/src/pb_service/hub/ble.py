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
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
from typing import Any, final

import anyio
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakGATTProtocolError

from pb_service.hub import protocol as p
from pb_service.hub.transport import (
    DeviceInfo,
    HubCommandError,
    HubLink,
    NotificationHandler,
    ScanFilter,
    UnavailableDevice,
)

logger = logging.getLogger(__name__)

__all__ = ["BleHubTransport", "device_id_for"]

SCHEME = "ble"

SCAN_RESPONSE_SECONDS = 1.0
"""How long a hub may go without a scan response before that is logged.

The kind of hub is in the scan response, which arrives after the advertisement,
so a hub is not reported until it has been heard. Every Pybricks firmware sends
one, so a hub that still has not after this long has a firmware bug, and is
logged as such rather than listed without its type.
"""


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
        or advertised something malformed, which is not yet, or a firmware
        bug if it never comes.
    """
    data = advertisement.service_data.get(p.PNP_ID_UUID)
    if data is None:
        return None

    try:
        return p.unpack_pnp_id(data)
    except ValueError:
        logger.debug("ignoring a malformed advertised PnP ID: %r", data)
        return None


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

    async def read_name(self) -> str:
        # Not a read of the Device Name characteristic, which macOS does not
        # allow. bleak asks the OS for the name instead, on every platform.
        return self._client.name

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

    @property
    def unavailable(self) -> tuple[UnavailableDevice, ...]:
        """Nothing: a radio that cannot be used fails outright.

        An adapter that is missing, switched off or not permitted stops the
        scan rather than hiding one hub among others, so it arrives as an
        exception out of :meth:`discover` and there is nothing left to report
        afterwards. Only a transport that enumerates devices one at a time, as
        USB does, can see one device and be turned away from it.
        """
        return ()

    async def discover(
        self, scan_filter: ScanFilter
    ) -> AsyncGenerator[DeviceInfo, None]:
        """Report Pybricks hubs as their advertisements arrive.

        Filtering on the Pybricks service UUID keeps other Bluetooth devices out
        of the results entirely, rather than showing them and hoping the user
        can tell which is theirs.

        Args:
            scan_filter: Which hubs to report.

        Yields:
            Hubs as they are seen. A hub may be reported more than once as its
            advertisement repeats; callers deduplicate by id. A hub is only
            reported once its scan response, which says what kind of hub it
            is, has arrived. One that never sends it is logged, not reported.
        """

        # aclosing on bleak's generator, because this one is nearly always
        # abandoned: a scan ends when the caller has seen enough, which closes
        # this generator at its yield below and leaves bleak's suspended inside
        # the loop. Its finally unregisters the radio's detection callback, and
        # without this that would run whenever it happened to be finalised, in
        # whatever task was running then, and after the scanner had stopped.
        async with (
            BleakScanner(service_uuids=[p.PYBRICKS_SERVICE_UUID]) as scanner,
            aclosing(scanner.advertisement_data()) as seen,
        ):
            first_seen: dict[str, float] = {}
            logged: set[str] = set()
            async for device, advertisement in seen:
                pnp = _advertised_pnp_id(advertisement)
                if pnp is None:
                    now = anyio.current_time()
                    since = now - first_seen.setdefault(device.address, now)
                    if since >= SCAN_RESPONSE_SECONDS and device.address not in logged:
                        logged.add(device.address)
                        logger.warning(
                            "ignoring %s (%s): no PnP ID in its scan response after"
                            " %.1f s, which is a firmware bug",
                            device.address,
                            advertisement.local_name or device.name,
                            since,
                        )
                    continue

                info = self._to_device_info(device, advertisement, pnp)
                if info is None:
                    continue

                if scan_filter.name is not None and info.name != scan_filter.name:
                    continue

                if info.id in scan_filter.exclude:
                    continue

                self._devices[info.id] = device
                yield info

    def _to_device_info(
        self,
        device: BLEDevice,
        advertisement: AdvertisementData,
        pnp: p.PnpId,
    ) -> DeviceInfo | None:
        # A hub may be heard before its name is, so one without a name yet is
        # skipped rather than shown as unnamed.
        name = advertisement.local_name or device.name
        if not name:
            return None

        return DeviceInfo(
            id=device_id_for(device.address),
            name=name,
            address=device.address,
            rssi=advertisement.rssi,
            transport=SCHEME,
            hub_type=p.hub_type_name(pnp),
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
