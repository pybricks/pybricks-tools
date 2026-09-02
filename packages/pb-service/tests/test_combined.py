# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Several transports behaving as one.

The test that matters most here is the one for stopping a scan early, because
that is what every caller does -- connecting takes the first hub that matches --
and getting it wrong took the service down rather than producing a wrong answer.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager

import anyio
import pytest

from pb_service.hub.combined import CombinedTransport
from pb_service.hub.transport import DeviceInfo, HubLink, ScanFilter

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Stub:
    """A transport that reports the hubs it was given, over and over.

    Composed rather than inherited: it satisfies the transport protocol by
    having the right members, which is all a Protocol asks for.
    """

    def __init__(self, scheme: str, *names: str, fails: bool = False) -> None:
        self.scheme = scheme
        self.on_disconnect: Callable[[str], None] | None = None
        self.opened: list[str] = []
        self.closed = False

        self._names = names
        self._fails = fails

    async def discover(
        self, scan_filter: ScanFilter
    ) -> AsyncGenerator[DeviceInfo, None]:
        if self._fails:
            raise OSError(f"the {self.scheme} transport cannot work here")

        try:
            while True:
                for name in self._names:
                    if scan_filter.name is not None and scan_filter.name != name:
                        continue

                    yield DeviceInfo(
                        id=f"{self.scheme}:{name}",
                        name=name,
                        address=name,
                        transport=self.scheme,
                    )

                # Once per round rather than after each hub, so that a filter
                # matching nothing still reaches a checkpoint. Without it this
                # stub spins without ever awaiting and takes the event loop
                # with it, which is a hang rather than a failure.
                await anyio.sleep(0.01)
        finally:
            self.closed = True

    @asynccontextmanager
    async def open(self, device_id: str) -> AsyncGenerator[HubLink, None]:
        self.opened.append(device_id)
        yield _StubLink()


class _StubLink:
    """Enough of a link to be handed back."""

    @property
    def is_connected(self) -> bool:
        return True

    async def read_characteristic(self, uuid: str) -> bytes:
        return b""

    async def write_characteristic(
        self, uuid: str, data: bytes, *, response: bool = True
    ) -> None:
        return None

    async def start_notify(self, uuid: str, handler: Callable[[bytes], None]) -> None:
        return None


async def _take(
    transport: CombinedTransport, count: int, name: str | None = None
) -> list[DeviceInfo]:
    """Take a few hubs from a scan and then stop, as every real caller does."""
    found: list[DeviceInfo] = []

    async with aclosing(transport.discover(ScanFilter(name=name))) as stream:
        async for device in stream:
            found.append(device)

            if len(found) == count:
                break

    return found


# ------------------------------------------------------------------ discovery


async def test_a_scan_reports_hubs_from_every_transport() -> None:
    usb, ble = _Stub("usb", "Bumblebee"), _Stub("ble", "Rover")
    transport = CombinedTransport(usb, ble)

    found = await _take(transport, 2)

    assert {device.id for device in found} == {"usb:Bumblebee", "ble:Rover"}


async def test_stopping_a_scan_early_does_not_raise() -> None:
    # The regression. Closing a generator throws GeneratorExit in at the yield,
    # and a task group's __aexit__ wraps anything that is not a cancellation in
    # an ExceptionGroup -- so this used to end the request, and with it the
    # service, every time a caller took the first hub and stopped.
    transport = CombinedTransport(_Stub("usb", "Bumblebee"), _Stub("ble", "Rover"))

    found = await _take(transport, 1)

    assert len(found) == 1


async def test_stopping_a_scan_closes_every_transport() -> None:
    # Whatever each transport was holding -- a radio, an open serial port --
    # has to be given back, and only closing its generator does that.
    usb, ble = _Stub("usb", "Bumblebee"), _Stub("ble", "Rover")

    await _take(CombinedTransport(usb, ble), 1)

    # No waiting for it: the composite's task group cannot finish unwinding
    # until every child generator has been closed, so this is already true by
    # the time the scan returns. Anything that needed a wait here would mean a
    # transport had been left running.
    assert (usb.closed, ble.closed) == (True, True)


async def test_a_transport_that_cannot_work_does_not_stop_the_others() -> None:
    # A machine with no Bluetooth adapter must still find a hub on a cable.
    # Before the transports were combined, that machine could not scan at all.
    working = _Stub("usb", "Bumblebee")
    transport = CombinedTransport(_Stub("ble", "Rover", fails=True), working)

    found = await _take(transport, 1)

    assert [device.id for device in found] == ["usb:Bumblebee"]


async def test_a_filter_reaches_every_transport() -> None:
    transport = CombinedTransport(_Stub("usb", "Bumblebee"), _Stub("ble", "Rover"))

    found = await _take(transport, 1, name="Rover")

    assert [device.id for device in found] == ["ble:Rover"]


# ------------------------------------------------------------------ opening


async def test_a_hub_is_opened_by_the_transport_its_id_belongs_to() -> None:
    usb, ble = _Stub("usb", "Bumblebee"), _Stub("ble", "Rover")
    transport = CombinedTransport(usb, ble)

    async with transport.open("ble:Rover"):
        pass

    assert (usb.opened, ble.opened) == ([], ["ble:Rover"])


async def test_an_id_no_transport_owns_is_refused() -> None:
    # An id from a service built with different transports -- one a client
    # stored and offered back later.
    transport = CombinedTransport(_Stub("usb", "Bumblebee"))

    with pytest.raises(ConnectionError, match="rfcomm:Rover"):
        transport.open("rfcomm:Rover")


# ------------------------------------------------------------ the plumbing


async def test_a_hub_a_transport_loses_is_passed_on() -> None:
    # The manager above needs to hear this, and only the transport knows it: a
    # hub switched off says nothing and simply stops answering.
    usb = _Stub("usb", "Bumblebee")
    transport = CombinedTransport(usb)
    lost: list[str] = []
    transport.on_disconnect = lost.append

    assert usb.on_disconnect is not None
    usb.on_disconnect("usb:Bumblebee")

    assert lost == ["usb:Bumblebee"]


async def test_the_scheme_names_every_transport_in_preference_order() -> None:
    transport = CombinedTransport(_Stub("usb"), _Stub("ble"))

    assert transport.scheme == "usb+ble"


async def test_combining_nothing_is_refused() -> None:
    # A service that cannot reach anything is a bug rather than a
    # configuration, and it should say so where it is built.
    with pytest.raises(ValueError, match="at least one"):
        CombinedTransport()


def test_open_returns_a_context_manager() -> None:
    transport = CombinedTransport(_Stub("usb", "Bumblebee"))

    assert isinstance(transport.open("usb:Bumblebee"), AbstractAsyncContextManager)


# ------------------------------------------------------- asking for one only


async def test_asking_for_one_transport_does_not_start_the_others() -> None:
    # Not merely a filter on the results: a scan for USB must not start the
    # radio, which costs time and takes the adapter for as long as it runs.
    usb, ble = _Stub("usb", "Bumblebee"), _Stub("ble", "Rover")
    transport = CombinedTransport(usb, ble)

    found: list[DeviceInfo] = []

    async with aclosing(transport.discover(ScanFilter(transport="usb"))) as stream:
        async for device in stream:
            found.append(device)
            break

    assert [device.id for device in found] == ["usb:Bumblebee"]
    assert ble.closed is False, "the Bluetooth transport was started anyway"


async def test_asking_for_a_transport_this_service_lacks_finds_nothing() -> None:
    # A search with no result rather than an error: it reads to the user as the
    # hub not being there, which is the same thing they would be told anyway.
    transport = CombinedTransport(_Stub("ble", "Rover"))

    found: list[DeviceInfo] = []

    async with aclosing(transport.discover(ScanFilter(transport="usb"))) as stream:
        async for device in stream:
            found.append(device)

    assert found == []
