# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Going back to a hub whose cable came out.

The property worth pinning is not that a lost hub is reconnected but that only
a *lost* one is: a hub somebody disconnected on purpose must stay disconnected,
and a service shutting down must not stop to wait for a cable. Both of those
end a connection by the same route as an unplugging does, so getting them wrong
would look like the feature working.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, aclosing
from pathlib import Path

import anyio
import pytest

from pb_service.hub.connection import HubConnection
from pb_service.hub.fake import FakeHub, FakeHubTransport
from pb_service.hub.manager import HubManager, run_hub_manager
from pb_service.hub.reconnect import wait_for_return
from pb_service.hub.selection import Selection
from pb_service.hub.transport import (
    DeviceInfo,
    HubLink,
    ScanFilter,
    UnavailableDevice,
    reconnects,
)

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


class _Cable:
    """The simulated transport, on a cable that can be pulled out.

    Composed rather than inherited: it holds a :class:`FakeHubTransport` for
    the link and the handshake, and adds only the two things these tests are
    about -- a transport called ``usb``, so that a lost hub is one worth
    waiting for, and a cable to pull.

    Its own ``discover`` replaces the simulated one because that reports each
    hub once and then waits to be stopped, which cannot express a hub being
    absent and then present.
    """

    scheme = "usb"

    def __init__(self, hub: FakeHub) -> None:
        """Plug a hub in.

        Args:
            hub: The hub on the other end.
        """
        self._hub = hub
        self._inner = FakeHubTransport(hub)
        self._inner.on_disconnect = self._dropped

        self.on_disconnect: Callable[[str], None] | None = None
        self.plugged_in = True
        self.opens = 0
        self.opened = anyio.Event()
        """Set each time the transport is opened, so a test can wait rather
        than look repeatedly."""

    def _dropped(self, device_id: str) -> None:
        if self.on_disconnect is not None:
            self.on_disconnect(device_id)

    @property
    def unavailable(self) -> tuple[UnavailableDevice, ...]:
        """Delegated: an unplugged cable is not a port we were refused."""
        return self._inner.unavailable

    @property
    def device(self) -> DeviceInfo:
        """The hub as a scan would report it.

        The id keeps the simulated hub's own prefix, since the manager hands
        ids straight back to the transport that produced them and never reads
        one itself.
        """
        return DeviceInfo(
            id=self._hub.device_id,
            name=self._hub.name,
            address=self._hub.address,
            transport=self.scheme,
        )

    def unplug(self) -> None:
        """Pull the cable out, as a person would."""
        self.plugged_in = False
        self._hub.disconnect()

    def plug_in(self) -> None:
        """Put it back."""
        self.opened = anyio.Event()
        self.plugged_in = True

    async def discover(
        self, scan_filter: ScanFilter
    ) -> AsyncGenerator[DeviceInfo, None]:
        while True:
            if self.plugged_in and scan_filter.transport in (None, self.scheme):
                yield self.device

            await anyio.sleep(0.01)

    def open(self, device_id: str) -> AbstractAsyncContextManager[HubLink]:
        self.opens += 1
        self.opened.set()
        return self._inner.open(device_id)


@pytest.fixture
async def cable(tmp_path: Path) -> AsyncGenerator[tuple[HubManager, _Cable], None]:
    plugged = _Cable(FakeHub())

    async with run_hub_manager(plugged) as manager:
        manager.selection = Selection(tmp_path / "selection.json")
        yield manager, plugged


async def _settle(manager: HubManager, device: DeviceInfo) -> None:
    """Wait for a reconnection in flight to have finished.

    Asking to connect, rather than looking at the registry over and over:
    ``connect`` holds the hub's own lock across the handshake and the registry
    entry, and hands back an existing connection rather than making a second
    one. So this waits for the reconnection without disturbing it, and the
    number of times the transport was opened says which of the two happened.
    """
    with anyio.fail_after(5):
        await manager.connect(device)


# --------------------------------------------------------------- the decision


def test_a_cable_is_worth_waiting_for_and_a_radio_is_not() -> None:
    # Not about the protocols: it is about what a hub reappearing means. A
    # cable was put back by somebody who wants to carry on; a hub heard again
    # may be one being handed to another person entirely.
    assert reconnects("usb")
    assert not reconnects("ble")


def test_a_transport_this_build_has_not_thought_about_is_not_waited_for() -> None:
    assert not reconnects("rfcomm")
    assert not reconnects("")


# ------------------------------------------------- what counts as going away


async def _ended(end: Callable[[HubConnection], None]) -> HubConnection:
    """Run a connection, end it in the given way, and hand it back.

    Run rather than merely built, because a connection owns streams that only
    its own unwinding closes, and an unclosed one fails the suite.

    Args:
        end: How to finish it, called once it is connected.

    Returns:
        The connection, after it has closed.
    """
    plugged = _Cable(FakeHub())
    connection = HubConnection(plugged, plugged.device)

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            await tg.start(connection.run)
            end(connection)
            await connection.wait_closed()

    return connection


async def test_a_hub_that_stops_answering_is_lost() -> None:
    connection = await _ended(lambda hub: hub.notify_disconnected())

    assert connection.lost


async def test_a_hub_that_was_disconnected_is_not_lost() -> None:
    connection = await _ended(lambda hub: hub.close("client requested disconnect"))

    assert not connection.lost


async def test_a_link_reporting_itself_gone_after_a_close_is_not_a_loss() -> None:
    # The ordering that decides it, and the one a transport can get wrong: a
    # link closed on request may well announce that it has closed, and if that
    # counted, the service would go back to every hub anybody disconnected.
    def close_then_report(hub: HubConnection) -> None:
        hub.close("client requested disconnect")
        hub.notify_disconnected()

    connection = await _ended(close_then_report)

    assert not connection.lost


# ----------------------------------------------------------------- the waiting


async def test_waiting_ends_when_the_hub_is_reachable_again() -> None:
    plugged = _Cable(FakeHub())
    plugged.unplug()

    async def put_it_back() -> None:
        await anyio.sleep(0.05)
        plugged.plug_in()

    # Bound before the task group rather than inside it: a group's __aexit__
    # may swallow what its body raised, so nothing assigned in one is certain
    # to exist afterwards.
    found: DeviceInfo | None = None

    async with anyio.create_task_group() as tg:
        tg.start_soon(put_it_back)

        with anyio.fail_after(5):
            found = await wait_for_return(plugged, plugged.device)

    assert found is not None
    assert found.id == plugged.device.id


async def test_waiting_ignores_a_different_hub() -> None:
    plugged = _Cable(FakeHub())
    other = DeviceInfo(id="usb:0000", name="Rover", address="0000", transport="usb")

    with anyio.move_on_after(0.1) as scope:
        await wait_for_return(plugged, other)

    assert scope.cancelled_caught, "it stopped waiting for a hub that never came back"


async def test_waiting_looks_only_on_the_transport_the_hub_was_lost_on() -> None:
    # Otherwise waiting for a cable would start the radio, which costs the
    # adapter for as long as the wait -- and the wait is not short.
    seen: list[ScanFilter] = []
    plugged = _Cable(FakeHub())
    real_discover = plugged.discover

    def recording(scan_filter: ScanFilter) -> AsyncGenerator[DeviceInfo, None]:
        seen.append(scan_filter)
        return real_discover(scan_filter)

    plugged.discover = recording  # pyright: ignore[reportAttributeAccessIssue]

    with anyio.fail_after(5):
        await wait_for_return(plugged, plugged.device)

    assert [scan_filter.transport for scan_filter in seen] == ["usb"]


async def test_a_search_that_ends_without_the_hub_gives_up() -> None:
    # A transport that watches for ever never does this; one that can stop
    # must not leave the caller waiting on a stream nobody will write to.
    class _Once:
        scheme = "usb"
        on_disconnect: Callable[[str], None] | None = None
        unavailable: tuple[UnavailableDevice, ...] = ()

        async def discover(
            self, scan_filter: ScanFilter
        ) -> AsyncGenerator[DeviceInfo, None]:
            return
            yield  # pragma: no cover

        def open(self, device_id: str) -> AbstractAsyncContextManager[HubLink]:
            raise AssertionError("nothing should be opened")

    device = DeviceInfo(id="usb:1", name="Bumblebee", address="1", transport="usb")

    with anyio.fail_after(5):
        assert await wait_for_return(_Once(), device) is None


# ------------------------------------------------------------- the whole thing


async def test_a_hub_whose_cable_came_out_is_connected_again(
    cable: tuple[HubManager, _Cable],
) -> None:
    manager, plugged = cable
    await manager.connect(plugged.device)
    connection = manager.get(None)

    plugged.unplug()

    with anyio.fail_after(5):
        await connection.wait_closed()

    assert manager.count == 0, "a hub being waited for must not look connected"

    plugged.plug_in()

    with anyio.fail_after(5):
        await plugged.opened.wait()

    await _settle(manager, plugged.device)

    assert manager.count == 1
    assert plugged.opens == 2, "the hub should have been opened once more, and once"


async def test_a_hub_disconnected_on_purpose_stays_disconnected(
    cable: tuple[HubManager, _Cable],
) -> None:
    # The one that would look like the feature working. A requested disconnect
    # ends the connection by the same route an unplugging does, and the
    # simulated transport reports the link closing either way, so only the
    # order of the two tells them apart.
    manager, plugged = cable
    await manager.connect(plugged.device)
    connection = manager.get(None)

    await manager.disconnect(plugged.device.id)

    with anyio.fail_after(5):
        await connection.wait_closed()

    # Long enough for a wait to have found the hub, which is still plugged in.
    await anyio.sleep(0.1)

    assert manager.count == 0
    assert plugged.opens == 1, "it went back to a hub nobody asked it to"


async def test_a_hub_lost_over_bluetooth_is_not_waited_for(
    tmp_path: Path,
) -> None:
    hub = FakeHub()
    transport = FakeHubTransport(hub)

    async with run_hub_manager(transport) as manager:
        manager.selection = Selection(tmp_path / "selection.json")

        async with aclosing(manager.discover(ScanFilter())) as stream:
            device = await anext(stream)

        await manager.connect(device)
        connection = manager.get(None)

        hub.disconnect()

        with anyio.fail_after(5):
            await connection.wait_closed()

        await anyio.sleep(0.1)

        assert manager.count == 0


async def test_the_service_can_still_shut_down_while_it_waits(
    tmp_path: Path,
) -> None:
    # The wait is unbounded, so leaving the manager has to cancel it. If it did
    # not, stopping the service would hang until somebody found the cable.
    plugged = _Cable(FakeHub())

    with anyio.fail_after(5):
        async with run_hub_manager(plugged) as manager:
            manager.selection = Selection(tmp_path / "selection.json")
            await manager.connect(plugged.device)
            plugged.unplug()
            await anyio.sleep(0.05)
