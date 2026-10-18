# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Bluetooth Classic, opened the first time RFCOMM needs it.

Opening it is not free and may not finish. On macOS it starts aio-rfcomm's
helper, which may ask the user for permission, and it does not return until the
system has said whether Bluetooth is usable -- which, on a Mac where nobody is
there to answer the prompt, is never. Done as the service started, that held up
USB and BLE as well. So it is done in a task of the transport's own scope, the
first time a scan or a connection asks for it, as bleak does for BLE.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from typing import Self, final
from uuid import UUID

import aio_rfcomm
import anyio
from aio_rfcomm.errors import BluetoothUnavailableError, RfcommError
from anyio.abc import TaskGroup

logger = logging.getLogger(__name__)

__all__ = ["LazyBluetooth"]


@final
class LazyBluetooth:
    """Stands in for :class:`aio_rfcomm.Bluetooth` until it is open.

    Only the calls the RFCOMM transport makes are offered, each waiting for the
    real one first.
    """

    def __init__(self, group: TaskGroup) -> None:
        """Make one that will open Bluetooth when first asked.

        Args:
            group: Owns the task holding Bluetooth open, so it closes when the
                transport's scope does.
        """
        self._group = group
        self._asked = False
        self._settled = anyio.Event()
        self._bluetooth: aio_rfcomm.Bluetooth | None = None
        self._failure: str | None = None

    async def _get(self) -> aio_rfcomm.Bluetooth:
        """Wait for Bluetooth to be open, opening it on the first call.

        Returns:
            The open Bluetooth object.

        Raises:
            BluetoothUnavailableError: It could not be opened at all.
        """
        if not self._asked:
            self._asked = True
            self._group.start_soon(self._hold)

        await self._settled.wait()

        if self._bluetooth is None:
            raise BluetoothUnavailableError(
                aio_rfcomm.Availability.UNSUPPORTED, self._failure
            )

        return self._bluetooth

    async def _hold(self) -> None:
        """Open Bluetooth and keep it open until the scope ends."""
        try:
            async with aio_rfcomm.open_bluetooth() as bluetooth:
                self._bluetooth = bluetooth
                self._settled.set()
                await anyio.sleep_forever()
        except RfcommError as exc:
            # Reported by each call from now on, as Bluetooth that cannot be
            # used, which is what RFCOMM already tells the user about.
            logger.warning("RFCOMM hubs will not be found: %s", exc)
            self._failure = str(exc)
            self._settled.set()

    async def list_known_devices(
        self, *, service: UUID | Collection[UUID] | None = None
    ) -> list[aio_rfcomm.RfcommDeviceInfo]:
        """As :meth:`aio_rfcomm.Bluetooth.list_known_devices`, once it is open.

        Args:
            service: The services to filter on, if any.

        Returns:
            The devices the system knows.
        """
        bluetooth = await self._get()
        return await bluetooth.list_known_devices(service=service)

    @asynccontextmanager
    async def open_service(
        self, device: aio_rfcomm.RfcommDeviceInfo, service: UUID
    ) -> AsyncGenerator[aio_rfcomm.RfcommChannel, None]:
        """As :meth:`aio_rfcomm.Bluetooth.open_service`, once it is open.

        Args:
            device: The device to connect to.
            service: The service to open.

        Yields:
            The open channel.
        """
        bluetooth = await self._get()

        async with bluetooth.open_service(device, service) as channel:
            yield channel

    def watch_availability(self) -> _Watcher:
        """As :meth:`aio_rfcomm.Bluetooth.watch_availability`, once it is open.

        Returns:
            Bluetooth's availability now, and then each change.
        """
        return _Watcher(self._get)


@final
class _Watcher:
    """Availability, from the real watcher once Bluetooth is open."""

    def __init__(self, get: Callable[[], Awaitable[aio_rfcomm.Bluetooth]]) -> None:
        self._get = get
        self._watching: aio_rfcomm.AvailabilityWatcher | None = None
        self._reported = False

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> aio_rfcomm.Availability:
        if self._watching is None:
            try:
                bluetooth = await self._get()
            except BluetoothUnavailableError:
                # Said once, as a watcher says the state now. It never changes
                # after, since nothing will open Bluetooth again.
                if not self._reported:
                    self._reported = True
                    return aio_rfcomm.Availability.UNSUPPORTED

                await anyio.sleep_forever()
                raise AssertionError("sleep_forever returned") from None

            self._watching = bluetooth.watch_availability()

        return await anext(self._watching)
