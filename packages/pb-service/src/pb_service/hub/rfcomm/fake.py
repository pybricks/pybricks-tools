# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Bluetooth with simulated hubs paired to it.

What this exists for is the half of the RFCOMM transport a real hub cannot
help with. A hub that is switched on answers every time, so the paths that
matter when something goes wrong -- a hub paired but switched off, a radio the
user has not allowed, a paired device that is not a hub at all, a hub that
stops answering mid-request -- are unreachable with hardware. Here each is one
argument.

The hub on the far end of a channel is :class:`~pb_service.hub.usb.fake.
FakeHubPort`, which answers the same framed protocol whatever carries it. That
it lives under ``usb`` is a leftover of being written there first; it belongs
beside the rest of the shared stream code, and moving it is a tidy-up this
does not wait for.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Collection, Sequence
from contextlib import asynccontextmanager
from typing import Any, Self, cast, final
from uuid import UUID

import aio_rfcomm
import anyio
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    DeviceNotFoundError,
    ServiceNotFoundError,
)

from pb_service.hub.rfcomm.port import PYBRICKS_RFCOMM_SERVICE, split_hub_name
from pb_service.hub.stream import cobs
from pb_service.hub.usb.fake import FakeHubPort

__all__ = ["FakePairedHub", "radio", "switch"]

_DELIMITER = bytes((cobs.DELIMITER,))


@final
class FakePairedHub:
    """A device this computer is paired with."""

    def __init__(
        self,
        *,
        name: str = "Bumblebee (MINDSTORMS EV3)",
        address: str = "00:16:53:4A:75:15",
        service: UUID = PYBRICKS_RFCOMM_SERVICE,
        reachable: bool = True,
        port: FakeHubPort | None = None,
    ) -> None:
        """Pair a device with the radio.

        Args:
            name: What the operating system calls it, suffix and all.
            address: Its Bluetooth address.
            service: The service class it answers on. Plain Serial Port makes
                it a device that is not necessarily a hub.
            reachable: Whether opening it succeeds. False is the common case
                this transport has to get right: a hub the computer is paired
                with but which is switched off.
            port: The hub on the far end, or None for a stock one.
        """
        self.name = name
        self.address = address
        self.service = service
        self.reachable = reachable
        self._port = port

    @property
    def port(self) -> FakeHubPort:
        """The hub on the far end, made only once something opens it.

        Lazily, because a hub holds streams that have to be closed again, and
        most of what this file is for never opens a channel at all.
        """
        if self._port is None:
            bare, _ = split_hub_name(self.name)
            self._port = FakeHubPort(name=bare or self.name)

        return self._port

    @property
    def info(self) -> aio_rfcomm.RfcommDeviceInfo:
        """How the radio describes it."""
        return aio_rfcomm.RfcommDeviceInfo(self.address, self.name)


@final
class _FakeChannel:
    """One frame at a time, as a radio hands them over."""

    def __init__(self, port: FakeHubPort) -> None:
        self._port = port

    async def receive_until(self, delimiter: bytes, *, max_bytes: int) -> bytes:
        return (await self._port.readuntil(delimiter))[: -len(delimiter)]

    async def send(self, data: bytes) -> None:
        await self._port.write(data)


@final
class _FakeWatcher:
    """Availability now, then each change, as the real watcher gives it."""

    def __init__(self, bluetooth: _FakeBluetooth) -> None:
        self._bluetooth = bluetooth
        self._last: aio_rfcomm.Availability | None = None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> aio_rfcomm.Availability:
        while True:
            changed = self._bluetooth.changed
            current = self._bluetooth.availability

            if current is not self._last:
                self._last = current
                return current

            await changed.wait()


@final
class _FakeBluetooth:
    def __init__(self, hubs: Sequence[FakePairedHub], unavailable: str | None) -> None:
        self._hubs = hubs
        self._unavailable = unavailable
        self.changed = anyio.Event()

    @property
    def availability(self) -> aio_rfcomm.Availability:
        if self._unavailable is None:
            return aio_rfcomm.Availability.USABLE

        return aio_rfcomm.Availability.SWITCHED_OFF

    def switch(self, unavailable: str | None) -> None:
        self._unavailable = unavailable
        self.changed.set()
        self.changed = anyio.Event()

    def watch_availability(self) -> _FakeWatcher:
        return _FakeWatcher(self)

    def _check(self) -> None:
        if self._unavailable is not None:
            raise BluetoothUnavailableError(
                aio_rfcomm.Availability.SWITCHED_OFF, self._unavailable
            )

    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[aio_rfcomm.RfcommDeviceInfo]:
        self._check()
        wanted = {service} if isinstance(service, UUID) else service
        return [
            hub.info for hub in self._hubs if wanted is None or hub.service in wanted
        ]

    @asynccontextmanager
    async def open_service(
        self, device: aio_rfcomm.RfcommDeviceInfo, service: UUID
    ) -> AsyncGenerator[Any, None]:
        self._check()
        hub = next((h for h in self._hubs if h.address == device.address), None)

        if hub is None or not hub.reachable:
            raise DeviceNotFoundError(device.address)

        if service != hub.service:
            raise ServiceNotFoundError(f"{device.address} does not offer {service}")

        port = hub.port

        try:
            yield _FakeChannel(port)
        finally:
            port.close()


def radio(*hubs: FakePairedHub, unavailable: str | None = None) -> aio_rfcomm.Bluetooth:
    """Build Bluetooth with these devices paired to it.

    Args:
        hubs: What the computer is paired with.
        unavailable: Why Bluetooth cannot be used, or None for usable. This is
            the switched-off or not-allowed case, which the transport has to
            report without stopping the other transports. Opening Bluetooth
            succeeds regardless, so it is every call that fails.

    Returns:
        Something to hand :class:`~pb_service.hub.rfcomm.transport.
        RfcommHubTransport`. Cast, because the real one is a class rather
        than a protocol and nothing structural can stand in for it.
    """
    return cast("aio_rfcomm.Bluetooth", _FakeBluetooth(hubs, unavailable))


def switch(bluetooth: aio_rfcomm.Bluetooth, unavailable: str | None) -> None:
    """Make Bluetooth usable or unusable while it is in use.

    Args:
        bluetooth: What :func:`radio` built.
        unavailable: Why Bluetooth cannot be used from now on, or None for
            usable again. Watchers hear of it at once, as they would of the
            user flipping the switch.
    """
    cast("_FakeBluetooth", bluetooth).switch(unavailable)
