# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""USB transport: a hub reached as an ordinary serial port.

The firmware presents a hub as a standard CDC ACM device, so this needs no
driver anywhere -- Windows' own ``usbser.sys``, ``/dev/ttyACM*`` on Linux. What
travels over that pipe is the same Pybricks protocol the radio carries, framed
with COBS and wrapped in an envelope that supplies what a byte stream lacks.

Two properties of the firmware shape everything here, and both were measured
rather than assumed:

**Subscribing is what connects.** ``pbio_usb_connection_is_active()`` wants the
port open, DTR asserted *and* an explicit subscription. Until then the hub does
not regard anyone as its host -- so it goes on advertising over Bluetooth and
goes on counting down to switching itself off.

**But reads work without it.** So discovery can open a port, ask a hub its name
and close again without taking the hub, without stopping it advertising and
without resetting its idle timer. That is what lets a listing be a listing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Final, final

import anyio

from pb_service.hub import protocol as p
from pb_service.hub.stream import wire
from pb_service.hub.stream.link import StreamLink
from pb_service.hub.transport import (
    DeviceInfo,
    HubLink,
    ScanFilter,
    UnavailableDevice,
)
from pb_service.hub.usb.port import (
    PortInfo,
    PortLister,
    PortOpener,
    list_hub_ports,
    open_hub_port,
)

logger = logging.getLogger(__name__)

__all__ = ["UsbHubTransport", "device_id_for"]

SCHEME: Final = "usb"

PROBE_PATIENCE_SECONDS: Final = 2.0
"""How long discovery spends naming one hub before giving up on it."""

POLL_SECONDS: Final = 1.0
"""How often discovery re-enumerates the serial ports.

A poll, and the repository's rule is to wait for events rather than ask
repeatedly, so this owes an explanation. What cannot be waited on is **device
arrival**: neither the operating system's device notifications (``WM_DEVICE-
CHANGE`` and a WinRT device watcher on Windows, netlink from udev on Linux) are
exposed by any serial library, and writing that watcher is a job for a sibling
``aio-`` library rather than for this file. Enumeration itself is only a
directory read, and a hub already seen is never re-opened, so the cost of a turn
is small. It also runs only while somebody is scanning.
"""


def device_id_for(port: PortInfo) -> str:
    """Return the service-facing id for a port.

    Args:
        port: What enumeration reported.

    Returns:
        The prefixed identifier, built from the hub's USB serial number when it
        has one. That is stable across replugging and across a port being
        renumbered, which neither ``COM5`` nor ``/dev/ttyACM0`` is. It is not
        the durable hub identity this repository wants -- it says nothing over
        Bluetooth -- but it is the best a route can offer.
    """
    return f"{SCHEME}:{port.serial_number or port.device}"


@final
class UsbHubTransport:
    """Finds hubs on the serial ports and opens connections to them."""

    def __init__(
        self,
        list_ports: PortLister = list_hub_ports,
        open_port: PortOpener = open_hub_port,
    ) -> None:
        """Create a transport.

        Both arguments default to the real serial ports, so nothing that only
        wants hubs has to know they exist. They are arguments because a hub on a
        real cable cannot be asked to refuse a command or to vanish halfway
        through one, and those are the paths worth testing.

        Args:
            list_ports: Where the ports that might be hubs come from.
            open_port: How one of them is opened.
        """
        self.on_disconnect: Callable[[str], None] | None = None
        self._list_ports = list_ports
        self._open_port = open_port
        self._named: dict[str, DeviceInfo] = {}
        self._unavailable: dict[str, str] = {}

    @property
    def scheme(self) -> str:
        return SCHEME

    @property
    def unavailable(self) -> tuple[UnavailableDevice, ...]:
        """Ports enumerating as LEGO hubs that this computer would not open.

        Rebuilt by each turn of the scan rather than accumulated, so a cable
        moved to a port that does work stops being reported.
        """
        return tuple(
            UnavailableDevice(address, reason)
            for address, reason in self._unavailable.items()
        )

    async def discover(
        self, scan_filter: ScanFilter
    ) -> AsyncGenerator[DeviceInfo, None]:
        """Report hubs on the serial ports until the caller stops listening.

        Args:
            scan_filter: Which hubs to report.

        Yields:
            Each hub currently plugged in, re-reported on every turn so that a
            caller sees it as present rather than only once.
        """
        complained: dict[str, str] = {}

        while True:
            # Emptied at the start of every turn rather than added to, so that
            # a port that has been unplugged, or that opens this time, stops
            # being reported without anything having to notice that it has.
            self._unavailable = {}

            for port in self._list_ports():
                device_id = device_id_for(port)

                if device_id in scan_filter.exclude:
                    continue

                info = self._named.get(device_id)

                if info is None:
                    found = await self._probe(port, device_id)

                    if isinstance(found, UnavailableDevice):
                        self._unavailable[found.address] = found.reason
                        continue

                    if found is None:
                        continue

                    self._named[device_id] = found
                    info = found

                if scan_filter.name is not None and info.name != scan_filter.name:
                    continue

                yield info

            # Once per change rather than once per turn: a scan looks again
            # every second, and a port that will not open will not open on any
            # of them, which would be a log of the same line for as long as the
            # scan ran.
            if self._unavailable != complained:
                complained = dict(self._unavailable)

                for address, reason in complained.items():
                    logger.warning("not allowed to open %s: %s", address, reason)

            await anyio.sleep(POLL_SECONDS)

    async def _probe(
        self, port: PortInfo, device_id: str
    ) -> DeviceInfo | UnavailableDevice | None:
        """Ask a hub what it is called, without becoming its host.

        Args:
            port: The port to ask.
            device_id: The id it will be reported under.

        Returns:
            What was learned; an :class:`UnavailableDevice` if the computer
            refused to open the port at all; or ``None`` if it opened and then
            did not answer -- which is what a port belonging to something other
            than a Pybricks hub looks like, and also what a hub already in use
            looks like.
        """
        # Declared out here because a task group's exit may replace whatever its
        # body raised, so nothing assigned inside one is certain to exist after.
        name: str | None = None
        raw_pnp: bytes | None = None

        try:
            with anyio.fail_after(PROBE_PATIENCE_SECONDS):
                async with self._open_port(port.device) as serial:
                    link = StreamLink(serial, device_id)

                    async with anyio.create_task_group() as tg:
                        tg.start_soon(link.run, None)

                        name = (
                            await link.read_characteristic(p.DEVICE_NAME_UUID)
                        ).decode("utf-8", "replace")
                        raw_pnp = await link.read_characteristic(p.PNP_ID_UUID)

                        tg.cancel_scope.cancel()
        except PermissionError as exc:
            # Caught ahead of OSError, which it is a kind of, because it is the
            # one failure here that says something about the computer rather
            # than about the port: on Linux the user is not in the group that
            # owns the device node, and on Windows another program already has
            # the port open. Either way there is a hub on the end of it, so a
            # scan that reports nothing owes the user this.
            # strerror rather than str, which repeats the device name that the
            # value already carries, and falling back to str for a library that
            # raised one without an errno behind it.
            return UnavailableDevice(port.device, exc.strerror or str(exc))
        except (OSError, ConnectionError, TimeoutError, ValueError) as exc:
            logger.debug("%s: not a hub we can talk to: %s", port.device, exc)
            return None

        if name is None or raw_pnp is None:
            return None

        hub_type: str | None = None

        with suppress(ValueError):
            hub_type = p.hub_type_name(p.unpack_pnp_id(raw_pnp))

        return DeviceInfo(
            id=device_id,
            name=name,
            address=port.device,
            rssi=None,
            transport=SCHEME,
            hub_type=hub_type,
        )

    @asynccontextmanager
    async def open(self, device_id: str) -> AsyncGenerator[HubLink, None]:
        """Connect to a hub, subscribing so that the hub knows it has a host.

        Args:
            device_id: An id previously reported by :meth:`discover`.

        Yields:
            The open link, which disconnects on exit.
        """
        port = next(
            (port for port in self._list_ports() if device_id_for(port) == device_id),
            None,
        )

        if port is None:
            raise ConnectionError(f"no hub is plugged in as {device_id}")

        async with self._open_port(port.device) as serial:
            link = StreamLink(serial, device_id)

            async with link.running(self._on_lost):
                try:
                    yield link
                finally:
                    # Unsubscribing hands the hub back: it stops regarding us as
                    # its host, so it advertises again and can be reached over
                    # Bluetooth. Shielded because it is worth doing even when
                    # the reason we are leaving is a cancellation.
                    with (
                        anyio.CancelScope(shield=True),
                        anyio.move_on_after(1.0),
                        suppress(OSError, ConnectionError),
                    ):
                        await serial.write(wire.encode_subscribe(subscribed=False))

    def _on_lost(self, device_id: str) -> None:
        """Report a hub that went away by itself."""
        self._named.pop(device_id, None)

        if self.on_disconnect is not None:
            self.on_disconnect(device_id)
