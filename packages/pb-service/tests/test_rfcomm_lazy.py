# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Bluetooth Classic opened the first time RFCOMM needs it, and not before.

On a Mac where nobody is there to answer the permission prompt, opening it
never finishes. Done as the service started, that stopped the service starting
at all, USB and BLE included, which is how CI's macOS runners found it.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aio_rfcomm
import anyio
import pytest
from aio_rfcomm.errors import BluetoothUnavailableError, RfcommError

from pb_service.hub.rfcomm.fake import FakePairedHub, radio
from pb_service.hub.rfcomm.lazy import LazyBluetooth

pytestmark = pytest.mark.anyio

EV3 = "00:16:53:4A:75:15"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


class _Opener:
    """Stands in for ``aio_rfcomm.open_bluetooth``, counting the opens."""

    def __init__(
        self, bluetooth: aio_rfcomm.Bluetooth | None, failure: str | None = None
    ) -> None:
        self.opens = 0
        self.open = anyio.Event()
        self._bluetooth = bluetooth
        self._failure = failure

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[aio_rfcomm.Bluetooth, None]:
        self.opens += 1

        if self._failure is not None:
            raise RfcommError(self._failure)

        if self._bluetooth is None:
            # What a Mac with an unanswered permission prompt does.
            await anyio.sleep_forever()

        assert self._bluetooth is not None
        self.open.set()

        try:
            yield self._bluetooth
        finally:
            self.open = anyio.Event()


@pytest.fixture
def opener(monkeypatch: pytest.MonkeyPatch) -> _Opener:
    opener = _Opener(radio(FakePairedHub(address=EV3)))
    monkeypatch.setattr(aio_rfcomm, "open_bluetooth", opener)
    return opener


async def test_nothing_is_opened_until_asked(opener: _Opener) -> None:
    async with anyio.create_task_group() as tg:
        LazyBluetooth(tg)
        await anyio.sleep(0.05)
        tg.cancel_scope.cancel()

    assert opener.opens == 0


async def test_the_first_call_opens_it_and_later_ones_reuse_it(
    opener: _Opener,
) -> None:
    async with anyio.create_task_group() as tg:
        lazy = LazyBluetooth(tg)

        first = await lazy.list_known_devices()
        second = await lazy.list_known_devices()

        assert [d.address for d in first] == [EV3]
        assert first == second
        tg.cancel_scope.cancel()

    assert opener.opens == 1


async def test_bluetooth_closes_with_the_scope(opener: _Opener) -> None:
    async with anyio.create_task_group() as tg:
        lazy = LazyBluetooth(tg)
        await lazy.list_known_devices()
        assert opener.open.is_set()
        tg.cancel_scope.cancel()

    assert not opener.open.is_set()


async def test_availability_is_watched_through_the_real_one(opener: _Opener) -> None:
    async with anyio.create_task_group() as tg:
        lazy = LazyBluetooth(tg)

        with anyio.fail_after(5):
            now = await anext(lazy.watch_availability())

        assert now == aio_rfcomm.Availability.USABLE
        tg.cancel_scope.cancel()


async def test_failing_to_open_is_bluetooth_that_cannot_be_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What the RFCOMM transport already reports, rather than an error that
    # would end its scan.
    monkeypatch.setattr(aio_rfcomm, "open_bluetooth", _Opener(None, "no helper"))

    async with anyio.create_task_group() as tg:
        lazy = LazyBluetooth(tg)

        with pytest.raises(BluetoothUnavailableError, match="no helper"):
            await lazy.list_known_devices()

        watcher = lazy.watch_availability()
        assert await anext(watcher) == aio_rfcomm.Availability.UNSUPPORTED

        # Said once: nothing will open it again, so nothing changes.
        with anyio.move_on_after(0.05) as waited:
            await anext(watcher)

        assert waited.cancelled_caught
        tg.cancel_scope.cancel()
