# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Hub commands through the whole stack, against a simulated hub.

The client library, JSON-RPC, transport, service and hub logic are all real
here; only the radio is simulated. That means these run unchanged on a CI
machine with no Bluetooth adapter.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import aio_rfcomm
import anyio
import pytest

import pb_client
import pb_service.methods.hub
import pb_service.methods.program  # pyright: ignore[reportUnusedImport]

# Imported to register the RPC methods.
import pb_service.methods.service  # pyright: ignore[reportUnusedImport]
import pb_service.methods.stdio  # pyright: ignore[reportUnusedImport]
from pb_core.ipc import Endpoint
from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_service import service as service_module
from pb_service.hub import protocol as p
from pb_service.hub.combined import CombinedTransport
from pb_service.hub.fake import FakeHub, FakeHubTransport
from pb_service.hub.rfcomm.fake import FakePairedHub, radio, switch
from pb_service.hub.rfcomm.transport import RfcommHubTransport
from pb_service.hub.transport import (
    DeviceInfo,
    HubLink,
    ScanFilter,
    UnavailableDevice,
)

pytestmark = pytest.mark.anyio

FIRST = "Pybricks Hub"
SECOND = "Spare Hub"
PORT = "/dev/ttyACM0"
EV3 = "00:16:53:4A:75:15"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _private_runtime(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, short_tmp_path: Path
) -> Iterator[None]:
    # Short, not tmp_path: the service's socket goes in here, and on macOS a
    # path under tmp_path does not fit in sun_path. See short_tmp_path.
    runtime = short_tmp_path / "runtime"
    # 0700, because the service refuses a runtime directory other users can
    # read -- its socket and its token live there. A test that made one the
    # usual way would be refused, but only on POSIX, where the check means
    # something.
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    # And a state directory of its own, so that a test choosing a hub
    # does not rewrite the choice the person running the tests made.
    monkeypatch.setenv("PB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("PB_ENDPOINT", raising=False)
    yield


@pytest.fixture
def endpoint(short_tmp_path: Path) -> Endpoint:
    unique = uuid.uuid4().hex[:12]

    if sys.platform == "win32":
        return Endpoint("pipe", f"pb-hub-{unique}")

    return Endpoint("unix", str(short_tmp_path / f"{unique}.sock"))


@pytest.fixture
def hubs() -> list[FakeHub]:
    return [
        FakeHub(name=FIRST, address="00:11:22:33:44:55"),
        FakeHub(name=SECOND, address="66:77:88:99:AA:BB"),
    ]


@pytest.fixture
async def service_at(
    endpoint: Endpoint, hubs: list[FakeHub]
) -> AsyncGenerator[Endpoint, None]:
    transport = FakeHubTransport(*hubs)

    async with service_module.run_service(
        endpoint, idle_timeout=0, transport=transport
    ):
        yield endpoint


async def test_scan_finds_the_simulated_hubs(service_at: Endpoint) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        devices = await service.scan(seconds=0.3)

    assert {device.name for device in devices} == {FIRST, SECOND}


async def test_scan_can_filter_by_name(service_at: Endpoint) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        devices = await service.scan(seconds=0.3, name=SECOND)

    assert [device.name for device in devices] == [SECOND]


async def test_scanning_reports_a_hub_long_before_the_scan_ends(
    service_at: Endpoint,
) -> None:
    # The whole point of the streaming form: a hub is in hand as soon as it is
    # heard. The deadline here is a hundredth of the scan it asks for, so this
    # cannot pass by accident of waiting it out.
    async with pb_client.connect(service_at, autostart=False) as service:
        with anyio.fail_after(5):
            async with service.scanning(seconds=500) as devices:
                first = await anext(aiter(devices))

    assert first.name in {FIRST, SECOND}
    assert first.rssi is not None
    # Known without connecting: the hub advertises its Plug and Play ID in the
    # scan response, which is what lets a scan tell two hubs of different kinds
    # apart when they have the same name.
    assert first.hub_type == "Prime hub"


async def test_scanning_ends_by_itself_when_the_scan_is_over(
    service_at: Endpoint,
) -> None:
    # Nothing tells the caller's loop to stop except the scan finishing, so if
    # that signal is missing this hangs rather than fails -- hence the deadline.
    names: set[str] = set()

    async with pb_client.connect(service_at, autostart=False) as service:
        with anyio.fail_after(10):
            async with service.scanning(seconds=0.5) as devices:
                async for device in devices:
                    names.add(device.name)

    assert names == {FIRST, SECOND}


async def test_a_scan_with_no_time_limit_ends_when_the_caller_leaves(
    service_at: Endpoint,
) -> None:
    # What an interactive chooser asks for: the person may still be fetching
    # the hub from another room, so there is no deadline. What stops it is the
    # caller leaving, and this hangs rather than fails if that does not work.
    async with pb_client.connect(service_at, autostart=False) as service:
        with anyio.fail_after(5):
            async with service.scanning(seconds=None) as devices:
                first = await anext(aiter(devices))

    assert first.name in {FIRST, SECOND}


async def test_a_scan_that_reports_nothing_still_ends(service_at: Endpoint) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        with anyio.fail_after(10):
            async with service.scanning(seconds=0.3, name="No Such Hub") as devices:
                assert [device async for device in devices] == []


@pytest.fixture
async def bluetooth_off_at(endpoint: Endpoint) -> AsyncGenerator[Endpoint, None]:
    """A service whose Bluetooth adapter cannot be used.

    What RFCOMM reports with the radio switched off or not allowed: it cannot
    read the paired set at all, so it names the adapter rather than a hub.

    Args:
        endpoint: Where the service should listen.

    Yields:
        That endpoint, with the service running.
    """
    transport = FakeHubTransport()
    transport.hubs = []
    transport.unavailable = (
        UnavailableDevice(
            "bluetooth",
            "Bluetooth is switched off",
            kind="adapter",
            availability="switched_off",
        ),
    )

    async with service_module.run_service(
        endpoint, idle_timeout=0, transport=transport
    ):
        yield endpoint


async def test_bluetooth_that_cannot_be_used_is_not_reported_as_a_port(
    bluetooth_off_at: Endpoint,
) -> None:
    # Told a port would not open, somebody on Linux joins dialout and somebody
    # on a Mac unplugs a hub, and neither helps. The radio is what to fix.
    async with pb_client.connect(bluetooth_off_at, autostart=False) as service:
        with pytest.raises(JsonRpcError) as caught:
            await service.scan(seconds=0.3)

    assert caught.value.code == ErrorCode.BLUETOOTH_UNAVAILABLE
    assert "switched off" in str(caught.value), "the system's words are lost"
    # And in a word, which is what the client chooses its advice by.
    assert caught.value.data == {"availability": "switched_off"}


@pytest.fixture
async def asking_at(
    endpoint: Endpoint,
) -> AsyncGenerator[tuple[Endpoint, aio_rfcomm.Bluetooth], None]:
    """A service whose only hub is behind a permission dialog nobody has answered.

    What aio-rfcomm 1.1 gives on a Mac the first time pb-service uses
    Bluetooth Classic: it opens straight away and says it is waiting for the
    user. The real RFCOMM transport, on the simulated radio, so that what the
    user does -- answering -- is one call.

    Args:
        endpoint: Where the service should listen.

    Yields:
        That endpoint, with the service running, and the radio to answer with
        :func:`~pb_service.hub.rfcomm.fake.switch`.
    """
    bluetooth = radio(
        FakePairedHub(address=EV3),
        unavailable="macOS is asking whether this program may use Bluetooth",
        reason=aio_rfcomm.Availability.AWAITING_PERMISSION,
    )

    async with service_module.run_service(
        endpoint, idle_timeout=0, transport=RfcommHubTransport(bluetooth)
    ):
        yield endpoint, bluetooth


async def test_a_scan_waits_while_the_user_is_asked_and_then_carries_on(
    asking_at: tuple[Endpoint, aio_rfcomm.Bluetooth],
) -> None:
    # Told at once, so that they look for the dialog; given the time to read
    # it, rather than a scan that runs out meanwhile; and the hub listed once
    # they have answered, without being asked to scan again.
    endpoint, bluetooth = asking_at
    found: list[pb_client.DeviceInfo] = []

    async with (
        pb_client.connect(endpoint, autostart=False) as service,
        service.watching_adapters() as changes,
        anyio.create_task_group() as tg,
    ):

        async def scan() -> None:
            found.extend(await service.scan(seconds=0.3))

        tg.start_soon(scan)

        with anyio.fail_after(5):
            (asked,) = await anext(changes)

        assert asked.awaiting_permission
        assert "asking" in asked.reason, "the system's words are lost"

        # Well past the scan's own limit.
        await anyio.sleep(1)
        switch(bluetooth, None)

        with anyio.fail_after(5):
            assert await anext(changes) == ()

    assert [device.address for device in found] == [EV3]


async def test_connecting_carries_on_once_the_user_answers(
    asking_at: tuple[Endpoint, aio_rfcomm.Bluetooth],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pb connect is what somebody types the first time, so it is where the
    # dialog first appears.
    monkeypatch.setattr(pb_service.methods.hub, "DEFAULT_SCAN_SECONDS", 0.3)
    endpoint, bluetooth = asking_at
    connected: list[pb_client.HubInfo] = []

    async with (
        pb_client.connect(endpoint, autostart=False) as service,
        service.watching_adapters() as changes,
        anyio.create_task_group() as tg,
    ):

        async def connect() -> None:
            connected.append(await service.connect_hub(transport="rfcomm"))

        tg.start_soon(connect)

        with anyio.fail_after(5):
            (asked,) = await anext(changes)

        assert asked.awaiting_permission
        await anyio.sleep(1)
        switch(bluetooth, None)

    assert [hub.address for hub in connected] == [EV3]


async def test_a_search_queued_behind_one_held_by_the_dialog_is_not_held_too(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One scan at a time, so a search waits for the radio behind any other.
    # Until it has the radio, what the transports report is the other
    # search's: a USB user must not be told about a Bluetooth dialog, or kept
    # waiting while somebody else reads it.
    monkeypatch.setattr(pb_service.methods.hub, "PERMISSION_SECONDS", 1.0)
    bluetooth = radio(
        FakePairedHub(address=EV3),
        unavailable="macOS is asking whether this program may use Bluetooth",
        reason=aio_rfcomm.Availability.AWAITING_PERMISSION,
    )
    transport = CombinedTransport(RfcommHubTransport(bluetooth), FakeHubTransport())
    told: list[tuple[pb_client.UnusableAdapter, ...]] = []
    took = float("inf")

    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=transport),
        pb_client.connect(endpoint, autostart=False) as asked,
        pb_client.connect(endpoint, autostart=False) as queued,
        anyio.create_task_group() as tg,
    ):

        async def held() -> None:
            with pytest.raises(JsonRpcError):
                await asked.scan(seconds=0.2, transport="rfcomm")

        async def watch() -> None:
            async with queued.watching_adapters() as changes:
                async for change in changes:
                    told.append(change)

        tg.start_soon(held)
        tg.start_soon(watch)
        # Asked second, so that it queues behind the scan the dialog holds.
        await anyio.sleep(0.05)
        began = anyio.current_time()
        await queued.scan(seconds=0.3, transport="fake")
        took = anyio.current_time() - began
        tg.cancel_scope.cancel()

    assert told == [], "told about another search's Bluetooth dialog"
    assert took < 0.9, "held by another search's Bluetooth dialog"


async def test_a_question_nobody_answers_is_not_waited_on_for_ever(
    asking_at: tuple[Endpoint, aio_rfcomm.Bluetooth],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Mac with nobody at it leaves the dialog open for good. The search
    # gives up in the end, and says what it was waiting for.
    monkeypatch.setattr(pb_service.methods.hub, "PERMISSION_SECONDS", 0.3)
    endpoint, _ = asking_at

    async with pb_client.connect(endpoint, autostart=False) as service:
        with anyio.fail_after(5), pytest.raises(JsonRpcError) as caught:
            await service.scan(seconds=0.2)

    assert caught.value.code == ErrorCode.BLUETOOTH_UNAVAILABLE
    assert caught.value.data == {"availability": "awaiting_permission"}


@pytest.fixture
async def out_of_reach_at(endpoint: Endpoint) -> AsyncGenerator[Endpoint, None]:
    """A service whose only hub is behind a port it may not open.

    What a Linux machine looks like when the user is not in the group that
    owns the device node, and a Windows one when another program already has
    the port: the hub enumerates, so the service knows it is there, and every
    attempt to open it is refused.

    Args:
        endpoint: Where the service should listen.

    Yields:
        That endpoint, with the service running.
    """
    transport = FakeHubTransport()
    transport.hubs = []
    transport.unavailable = (UnavailableDevice(PORT, "Permission denied"),)

    async with service_module.run_service(
        endpoint, idle_timeout=0, transport=transport
    ):
        yield endpoint


async def test_a_hub_that_would_not_open_is_not_reported_as_missing(
    out_of_reach_at: Endpoint,
) -> None:
    # The failure this exists for. Told that no hub was found, somebody plugs
    # the cable in again, tries another cable and switches the hub off and on,
    # because all of those are what "not found" means; nothing about it points
    # at the computer, which is where the problem is.
    async with pb_client.connect(out_of_reach_at, autostart=False) as service:
        with pytest.raises(JsonRpcError) as caught:
            await service.scan(seconds=0.3)

    assert caught.value.code == ErrorCode.SERIAL_PORT_UNAVAILABLE
    assert PORT in str(caught.value), "the message does not say which port"


async def test_connecting_says_the_same_thing(
    out_of_reach_at: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Connecting is what a user actually types, and it scans first, so it
    # reaches the same dead end by a different route.
    monkeypatch.setattr(pb_service.methods.hub, "DEFAULT_SCAN_SECONDS", 0.3)

    async with pb_client.connect(out_of_reach_at, autostart=False) as service:
        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub()

    assert caught.value.code == ErrorCode.SERIAL_PORT_UNAVAILABLE


async def test_a_scan_that_finds_a_hub_says_nothing_about_a_refused_port(
    endpoint: Endpoint, hubs: list[FakeHub]
) -> None:
    # A port that will not open is only worth raising when it is the whole
    # answer. Reachable hubs are the answer to the question that was asked.
    transport = FakeHubTransport(*hubs)
    transport.unavailable = (UnavailableDevice(PORT, "Permission denied"),)

    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=transport),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        devices = await service.scan(seconds=0.3)

    assert {device.name for device in devices} == {FIRST, SECOND}


async def test_connect_and_describe(service_at: Endpoint) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        info = await service.connect_hub(name=FIRST)
        assert info.name == FIRST
        assert info.state == "connected"
        assert info.protocol == "1.5.0"
        assert info.num_slots == 5
        # Read from the hub's PnP ID during the handshake, and named in the
        # words Pybricks Code uses.
        assert info.hub_type == "Prime hub"


async def test_an_unfamiliar_hub_is_left_unnamed(
    endpoint: Endpoint, tmp_path: Path
) -> None:
    # A hub this version has never heard of still works; it just does not get
    # called anything, which is better than calling it the wrong thing.
    hub = FakeHub(name=FIRST, address="00:11:22:33:44:55", hub_type=0x99)

    async with (
        service_module.run_service(
            endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
        ),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        info = await service.connect_hub(name=FIRST)

    assert info.hub_type is None
    assert info.state == "connected"


async def test_a_hub_outlives_the_client_that_connected_it(
    service_at: Endpoint,
) -> None:
    # The premise of the design, at the level a user sees it: connect in one
    # command, and a completely separate command still finds it connected.
    async with pb_client.connect(service_at, autostart=False) as first:
        await first.connect_hub(name=FIRST)

    async with pb_client.connect(service_at, autostart=False) as second:
        hubs, default = await second.list_hubs()

    assert [hub.name for hub in hubs] == [FIRST]
    assert default == hubs[0].id


async def test_connecting_twice_reuses_the_connection(service_at: Endpoint) -> None:
    # Reconnecting over Bluetooth is slow, so a second request for a hub we
    # already hold must not start a new connection.
    async with pb_client.connect(service_at, autostart=False) as service:
        first = await service.connect_hub(name=FIRST)
        second = await service.connect_hub(name=FIRST)
        assert first.id == second.id
        hubs, _ = await service.list_hubs()
        assert len(hubs) == 1


async def test_several_hubs_at_once(service_at: Endpoint) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.connect_hub(name=FIRST)
        await service.connect_hub(name=SECOND)
        hubs, default = await service.list_hubs()

    assert {hub.name for hub in hubs} == {FIRST, SECOND}
    # The most recently connected hub becomes the default.
    assert default == next(hub.id for hub in hubs if hub.name == SECOND)


async def test_scan_hides_hubs_that_are_already_connected(
    service_at: Endpoint,
) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.connect_hub(name=FIRST)
        devices = await service.scan(seconds=0.3)

    assert [device.name for device in devices] == [SECOND]


async def test_choosing_the_hub_commands_act_on(service_at: Endpoint) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        first = await service.connect_hub(name=FIRST)
        await service.connect_hub(name=SECOND)

        # An id is accepted, since it is what the wire calls the parameter,
        # but what comes back -- and what is stored -- is the hub's name.
        chosen = await service.use_hub(first.id)
        assert chosen.name == FIRST
        assert chosen.connected

        # A command with no hub named now acts on that one.
        info = await service.hub_info()
        assert info.name == FIRST


async def test_the_choice_is_reported_without_being_changed(
    service_at: Endpoint,
) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        assert (await service.chosen_hub()).name is None

        await service.connect_hub(name=SECOND)
        await service.use_hub(FIRST)

        reported = await service.chosen_hub()
        assert reported.name == FIRST
        # Chosen, but switched off as far as anyone here can tell, which is
        # worth saying rather than implying by silence.
        assert not reported.connected
        assert (await service.chosen_hub()).name == FIRST


async def test_a_hub_can_be_chosen_before_it_is_connected(
    service_at: Endpoint,
) -> None:
    # Pinning the hub a project belongs to is a reasonable thing to do before
    # any of the hardware is switched on, so a name that matches nothing yet
    # is accepted rather than refused.
    async with pb_client.connect(service_at, autostart=False) as service:
        chosen = await service.use_hub("Some Other Hub")
        assert chosen.name == "Some Other Hub"
        assert not chosen.connected

        # It does not stand in the way of the only hub there is.
        await service.connect_hub(name=FIRST)
        assert (await service.hub_info()).name == FIRST


async def test_connecting_with_no_hub_named_connects_the_chosen_one(
    service_at: Endpoint,
) -> None:
    # pybricks/support#2866: choosing a hub and then connecting reached
    # whichever hub advertised first, which need not be the one chosen.
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.use_hub(SECOND)
        info = await service.connect_hub()

    assert info.name == SECOND


async def test_the_chosen_hub_is_connected_even_with_another_connected(
    service_at: Endpoint,
) -> None:
    # Handing back the hub already connected instead would leave one hub
    # chosen and another connected, and commands going to the wrong one.
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.connect_hub(name=FIRST)
        await service.use_hub(SECOND)
        info = await service.connect_hub()
        hubs, _ = await service.list_hubs()

    assert info.name == SECOND
    assert {hub.name for hub in hubs} == {FIRST, SECOND}


async def test_a_chosen_hub_already_connected_is_handed_back(
    service_at: Endpoint,
) -> None:
    # It has stopped advertising, so looking for it would report it missing.
    async with pb_client.connect(service_at, autostart=False) as service:
        first = await service.connect_hub(name=SECOND)
        await service.use_hub(SECOND)
        again = await service.connect_hub()

    assert again.id == first.id
    assert not first.already_connected
    assert again.already_connected


async def test_a_chosen_hub_that_is_switched_off_is_named_as_missing(
    service_at: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two other hubs are switched on, and neither is taken instead: the user
    # chose, and connecting to a hub they did not choose is the bug.
    monkeypatch.setattr(pb_service.methods.hub, "DEFAULT_SCAN_SECONDS", 0.3)

    async with pb_client.connect(service_at, autostart=False) as service:
        await service.use_hub("Some Other Hub")

        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub()

        hubs, _ = await service.list_hubs()

    assert caught.value.code == ErrorCode.HUB_NOT_FOUND
    assert "Some Other Hub" in str(caught.value)
    assert hubs == []


async def test_several_hubs_and_nothing_chosen_is_refused(
    service_at: Endpoint,
) -> None:
    # In a classroom the first hub to advertise is usually somebody else's.
    async with pb_client.connect(service_at, autostart=False) as service:
        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub()

        hubs, _ = await service.list_hubs()

    assert caught.value.code == ErrorCode.HUB_AMBIGUOUS
    offered = {device["name"] for device in caught.value.data["devices"]}
    assert offered == {FIRST, SECOND}
    assert hubs == []


@pytest.fixture
async def twins_at(endpoint: Endpoint) -> AsyncGenerator[Endpoint, None]:
    """Two hubs flashed without a name, so both answer to the default one."""
    transport = FakeHubTransport(
        FakeHub(name=FIRST, address="00:11:22:33:44:55"),
        FakeHub(name=FIRST, address="66:77:88:99:AA:BB"),
    )

    async with service_module.run_service(
        endpoint, idle_timeout=0, transport=transport
    ):
        yield endpoint


async def test_a_chosen_name_two_hubs_share_is_refused(twins_at: Endpoint) -> None:
    # Choosing by name is no choice at all when every hub has the same one.
    async with pb_client.connect(twins_at, autostart=False) as service:
        await service.use_hub(FIRST)

        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub()

        hubs, _ = await service.list_hubs()

    assert caught.value.code == ErrorCode.HUB_AMBIGUOUS
    assert caught.value.data["name"] == FIRST
    assert len(caught.value.data["devices"]) == 2
    assert hubs == []


async def test_a_name_given_that_two_hubs_share_is_refused(twins_at: Endpoint) -> None:
    async with pb_client.connect(twins_at, autostart=False) as service:
        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub(name=FIRST)

    assert caught.value.code == ErrorCode.HUB_AMBIGUOUS
    assert caught.value.data["name"] == FIRST


async def test_an_id_tells_hubs_with_one_name_apart(twins_at: Endpoint) -> None:
    async with pb_client.connect(twins_at, autostart=False) as service:
        devices = await service.scan(seconds=0.3)
        info = await service.connect_hub(hub_id=devices[1].id)

    assert info.id == devices[1].id


async def test_a_name_given_for_a_connected_hub_hands_it_back(
    twins_at: Endpoint,
) -> None:
    # The connected twin has stopped advertising, so a scan for the name would
    # find only the other one and connect it too, as if it were the one meant.
    async with pb_client.connect(twins_at, autostart=False) as service:
        devices = await service.scan(seconds=0.3)
        first = await service.connect_hub(hub_id=devices[0].id)
        again = await service.connect_hub(name=FIRST)
        hubs, _ = await service.list_hubs()

    assert again.id == first.id
    assert again.already_connected
    assert [hub.id for hub in hubs] == [first.id]


async def test_a_twin_heard_late_is_still_noticed(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Measured on real hubs: one advertising every 0.8 s or so is first heard
    # that long after the other, and deciding at the first one heard connected
    # to whichever that was without asking. The wait is the point here, so it
    # is the real one rather than the root conftest's.
    monkeypatch.setattr(pb_service.methods.hub, "AMBIGUITY_SECONDS", 1.0)
    transport = FakeHubTransport(
        FakeHub(name=FIRST, address="00:11:22:33:44:55"),
        FakeHub(name=FIRST, address="66:77:88:99:AA:BB", heard_after=0.8),
    )

    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=transport),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub(name=FIRST)

    assert caught.value.code == ErrorCode.HUB_AMBIGUOUS
    assert len(caught.value.data["devices"]) == 2


class _PluggedIn:
    """Hubs on cables, one per port, heard a moment into a search.

    Only as much of a transport as a search needs: these hubs are refused
    before anything opens them.
    """

    scheme = "usb"
    unavailable: tuple[UnavailableDevice, ...] = ()

    def __init__(self, *names: str, heard_after: float = 0.0) -> None:
        self.on_disconnect: Callable[[str], None] | None = None
        self._names = names
        self._heard_after = heard_after

    async def discover(
        self, scan_filter: ScanFilter
    ) -> AsyncGenerator[DeviceInfo, None]:
        await anyio.sleep(self._heard_after)

        for number, name in enumerate(self._names):
            port = f"/dev/ttyACM{number}"

            if scan_filter.name in (None, name):
                yield DeviceInfo(
                    id=f"usb:{port}",
                    name=name,
                    address=port,
                    transport=self.scheme,
                )

            # A port takes a moment to answer the first time it is opened.
            await anyio.sleep(0.01)

        await anyio.sleep_forever()

    def open(self, device_id: str) -> AbstractAsyncContextManager[HubLink]:
        raise ConnectionError(f"not expected to open {device_id}")


async def test_a_cable_does_not_hide_another_hub_heard_first(
    endpoint: Endpoint,
) -> None:
    # A hub on a cable used to end the search at once, and only two hubs on one
    # transport counted as two. So with one hub heard over the air and another
    # plugged in, the one on the cable was connected without asking.
    transport = CombinedTransport(
        _PluggedIn(FIRST, heard_after=0.02),
        FakeHubTransport(FakeHub(name=SECOND, address="66:77:88:99:AA:BB")),
    )

    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=transport),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub()

    assert caught.value.code == ErrorCode.HUB_AMBIGUOUS
    assert {d["name"] for d in caught.value.data["devices"]} == {FIRST, SECOND}


async def test_two_hubs_on_cables_are_two_hubs(endpoint: Endpoint) -> None:
    # USB is the best route there is, so finding a hub on it used to end the
    # search before the next port was read.
    transport = CombinedTransport(_PluggedIn(FIRST, FIRST))

    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=transport),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub()

    assert caught.value.code == ErrorCode.HUB_AMBIGUOUS
    assert len(caught.value.data["devices"]) == 2


async def test_a_paired_hub_switched_off_is_not_another_hub(
    endpoint: Endpoint,
) -> None:
    # RFCOMM lists what the computer is paired with, switched on or not, so an
    # EV3 in a drawer must not make the hub on the desk ambiguous.
    bluetooth = radio(FakePairedHub(address=EV3, reachable=False))
    transport = CombinedTransport(
        RfcommHubTransport(bluetooth),
        FakeHubTransport(FakeHub(name=SECOND, address="66:77:88:99:AA:BB")),
    )

    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=transport),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        info = await service.connect_hub()

    assert info.name == SECOND


async def test_a_connected_hub_asked_for_exclusively_is_refused(
    service_at: Endpoint,
) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.connect_hub(name=FIRST)

        with pytest.raises(JsonRpcError) as caught:
            await service._peer.call(  # pyright: ignore[reportPrivateUsage]
                "hub.connect", {"name": FIRST, "exclusive": True}
            )

    assert caught.value.code == ErrorCode.HUB_ALREADY_CONNECTED


async def test_a_connected_hub_asked_for_by_another_route_is_looked_for(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Which route is in use decides how quick everything after it is, so a hub
    # connected one way and asked for another is not the hub asked for.
    monkeypatch.setattr(pb_service.methods.hub, "DEFAULT_SCAN_SECONDS", 0.3)
    # Combined, because that is what keeps a search to the transport asked for.
    transport = CombinedTransport(
        FakeHubTransport(FakeHub(name=FIRST, address="00:11:22:33:44:55"))
    )

    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=transport),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        await service.connect_hub(name=FIRST)

        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub(name=FIRST, transport="usb")

    assert caught.value.code == ErrorCode.HUB_NOT_FOUND


@pytest.fixture
def drawer_and_desk(monkeypatch: pytest.MonkeyPatch) -> CombinedTransport:
    """A paired EV3 switched off, and two hubs heard a little apart.

    The waits are scaled down from the real ones but keep their proportions:
    the first hub heard comes after the paired one is listed, and the second
    after AMBIGUITY_SECONDS has passed since the paired one was listed, though
    not since the first hub was heard.
    """
    monkeypatch.setattr(pb_service.methods.hub, "AMBIGUITY_SECONDS", 0.6)
    monkeypatch.setattr(pb_service.methods.hub, "PAIRED_SECONDS", 2.0)

    # 0.1 s either side of the wait that a paired hub would have started, and
    # 0.2 s inside the one the first hub heard starts.
    return CombinedTransport(
        RfcommHubTransport(radio(FakePairedHub(address=EV3, reachable=False))),
        FakeHubTransport(
            FakeHub(name=FIRST, address="00:11:22:33:44:55", heard_after=0.3),
            FakeHub(name=SECOND, address="66:77:88:99:AA:BB", heard_after=0.7),
        ),
    )


async def test_a_paired_hub_does_not_cut_short_the_wait_for_another(
    endpoint: Endpoint, drawer_and_desk: CombinedTransport
) -> None:
    # The paired EV3 is listed at once. Had that started the wait, it would
    # have ended before the second hub was heard, and the first been taken.
    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=drawer_and_desk),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub()

    assert caught.value.code == ErrorCode.HUB_AMBIGUOUS
    assert {d["name"] for d in caught.value.data["devices"]} == {FIRST, SECOND}


async def test_a_hub_heard_after_the_wait_a_paired_one_would_start_is_found(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Had the paired EV3 started the wait, it would have ended holding only
    # the EV3, and tried to connect to a hub in a drawer.
    monkeypatch.setattr(pb_service.methods.hub, "AMBIGUITY_SECONDS", 0.6)
    monkeypatch.setattr(pb_service.methods.hub, "PAIRED_SECONDS", 2.0)
    transport = CombinedTransport(
        RfcommHubTransport(radio(FakePairedHub(address=EV3, reachable=False))),
        FakeHubTransport(
            FakeHub(name=SECOND, address="66:77:88:99:AA:BB", heard_after=0.9)
        ),
    )

    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=transport),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        info = await service.connect_hub()

    assert info.name == SECOND


async def test_a_permission_dialog_does_not_cut_short_the_wait_for_another(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Waiting on the dialog moves the end of the search later. Had the wait for
    # a second hub been bounded by the end as it was at the start, the first
    # hub heard after that would have ended the search at once.
    monkeypatch.setattr(pb_service.methods.hub, "DEFAULT_SCAN_SECONDS", 0.3)
    monkeypatch.setattr(pb_service.methods.hub, "AMBIGUITY_SECONDS", 0.5)
    monkeypatch.setattr(pb_service.methods.hub, "PERMISSION_SECONDS", 3.0)
    asking = radio(
        unavailable="macOS is asking whether this program may use Bluetooth",
        reason=aio_rfcomm.Availability.AWAITING_PERMISSION,
    )
    transport = CombinedTransport(
        RfcommHubTransport(asking),
        FakeHubTransport(
            FakeHub(name=FIRST, address="00:11:22:33:44:55", heard_after=0.8),
            FakeHub(name=SECOND, address="66:77:88:99:AA:BB", heard_after=0.9),
        ),
    )

    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=transport),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub()

    assert caught.value.code == ErrorCode.HUB_AMBIGUOUS


async def test_nothing_named_finds_the_connected_hub_on_the_route_asked_for(
    endpoint: Endpoint,
) -> None:
    # The hub a command would act on is the one connected last, but asked for
    # another route, a connected hub on it is the one meant.
    bluetooth = radio(FakePairedHub(address=EV3))
    transport = CombinedTransport(
        RfcommHubTransport(bluetooth),
        FakeHubTransport(FakeHub(name=SECOND, address="66:77:88:99:AA:BB")),
    )

    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=transport),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        ev3 = await service.connect_hub(transport="rfcomm")
        await service.connect_hub(name=SECOND)
        again = await service.connect_hub(transport="rfcomm")

    assert again.id == ev3.id
    assert again.already_connected


async def test_nothing_named_and_a_route_asked_for_keeps_the_hub_commands_use(
    service_at: Endpoint,
) -> None:
    # Two hubs connected the same way: asking for that way is no reason to
    # hand back a different one from the hub the next command will act on.
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.connect_hub(name=FIRST)
        second = await service.connect_hub(name=SECOND)
        again = await service.connect_hub(transport="fake")

    assert again.id == second.id


async def test_a_connected_id_with_another_route_asked_for_is_handed_back(
    service_at: Endpoint,
) -> None:
    # An id names its route too, so no hub on another one could have it, and
    # searching there would only end in "no hub found" with the hub connected.
    async with pb_client.connect(service_at, autostart=False) as service:
        first = await service.connect_hub(name=FIRST)

        with anyio.fail_after(1):
            again = await service.connect_hub(hub_id=first.id, transport="rfcomm")

    assert again.id == first.id
    assert again.already_connected


async def test_nothing_named_and_another_route_asked_for_is_looked_for(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As for a name: a hub connected one way is not one asked for another.
    monkeypatch.setattr(pb_service.methods.hub, "DEFAULT_SCAN_SECONDS", 0.3)
    transport = CombinedTransport(
        FakeHubTransport(FakeHub(name=FIRST, address="00:11:22:33:44:55"))
    )

    async with (
        service_module.run_service(endpoint, idle_timeout=0, transport=transport),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        await service.connect_hub()

        with pytest.raises(JsonRpcError) as caught:
            await service.connect_hub(transport="usb")

    assert caught.value.code == ErrorCode.HUB_NOT_FOUND


async def test_nothing_named_and_exclusive_refuses_a_connected_hub(
    service_at: Endpoint,
) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.connect_hub(name=FIRST)

        with pytest.raises(JsonRpcError) as caught:
            await service._peer.call(  # pyright: ignore[reportPrivateUsage]
                "hub.connect", {"exclusive": True}
            )

    assert caught.value.code == ErrorCode.HUB_ALREADY_CONNECTED


async def test_the_only_hub_switched_on_needs_no_name(endpoint: Endpoint) -> None:
    hub = FakeHub(name=FIRST, address="00:11:22:33:44:55")

    async with (
        service_module.run_service(
            endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
        ),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        info = await service.connect_hub()

    assert info.name == FIRST


async def test_an_id_for_a_hub_that_is_not_connected_is_refused(
    service_at: Endpoint,
) -> None:
    # A name for an absent hub is a choice; an id for one is nothing that can
    # be stored, since the name it would have to be turned into is unknown.
    async with pb_client.connect(service_at, autostart=False) as service:
        with pytest.raises(JsonRpcError) as caught:
            await service.use_hub("ble:00:00:00:00:00:00")

    assert caught.value.code == ErrorCode.HUB_NOT_FOUND


async def test_disconnecting_does_not_move_the_choice(service_at: Endpoint) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.connect_hub(name=FIRST)
        await service.connect_hub(name=SECOND)
        await service.use_hub(FIRST)

        await service.disconnect_hub(FIRST)

        # The hub was switched off, not un-chosen. Moving the choice to
        # whatever else happened to be connected is how a remembered
        # preference stops being worth remembering.
        assert (await service.chosen_hub()).name == FIRST
        # Meanwhile commands still have somewhere to go: the chosen hub is
        # not connected, so the only hub that is answers instead.
        assert (await service.hub_info()).name == SECOND


async def test_the_choice_can_be_forgotten(service_at: Endpoint) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.connect_hub(name=FIRST)
        await service.use_hub(FIRST)

        cleared = await service.use_hub(None)
        assert cleared.name is None
        assert (await service.chosen_hub()).name is None


async def test_a_hub_can_be_named_by_name(service_at: Endpoint) -> None:
    # A name is all "pb --hub" ever passes, because a hub id is built from the
    # Bluetooth address, which is not stable and is not shown to anyone. Every
    # method that takes a hub has to accept one.
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.connect_hub(name=FIRST)
        await service.connect_hub(name=SECOND)

        info = await service.hub_info(FIRST)
        assert info.name == FIRST

        # Not merely the default: SECOND was connected last and holds it.
        await service.disconnect_hub(FIRST)
        hubs, _ = await service.list_hubs()
        assert [hub.name for hub in hubs] == [SECOND]


async def test_disconnect_removes_the_hub(service_at: Endpoint) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        info = await service.connect_hub(name=FIRST)
        await service.disconnect_hub(info.id)
        hubs, _ = await service.list_hubs()

    assert hubs == []


async def test_commands_for_an_unknown_hub_are_reported(service_at: Endpoint) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        with pytest.raises(JsonRpcError) as info:
            await service.hub_info("fake:NO:SUCH:HUB")

        assert info.value.code == ErrorCode.HUB_NOT_FOUND


async def test_connecting_to_a_hub_that_is_not_there(
    service_at: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pb_service.methods.hub, "DEFAULT_SCAN_SECONDS", 0.3)

    async with pb_client.connect(service_at, autostart=False) as service:
        with pytest.raises(JsonRpcError) as info:
            await service.connect_hub(name="No Such Hub")

        assert info.value.code == ErrorCode.HUB_NOT_FOUND
        assert info.value.message == "no hub found"


async def test_stop_program_reaches_the_hub(
    service_at: Endpoint, hubs: list[FakeHub]
) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.connect_hub(name=FIRST)
        await service.stop_program()

    assert hubs[0].commands[-1] == bytes([p.Command.STOP_USER_PROGRAM])


async def test_status_reflects_a_running_program(
    service_at: Endpoint, hubs: list[FakeHub]
) -> None:
    async with pb_client.connect(service_at, autostart=False) as service:
        await service.connect_hub(name=FIRST)
        hubs[0].set_program_running(True)

        with anyio.fail_after(5):
            while True:
                info = await service.hub_info()
                if info.program_running:
                    break

                await anyio.sleep(0.02)

        assert "USER_PROGRAM_RUNNING" in info.status_names


async def test_a_connected_hub_keeps_the_service_alive(
    endpoint: Endpoint, hubs: list[FakeHub]
) -> None:
    # Holding the connection open with nobody attached is the entire point, so
    # the idle timeout must not fire while a hub is connected.
    transport = FakeHubTransport(*hubs)

    async with service_module.run_service(
        endpoint, idle_timeout=0.4, transport=transport
    ) as service:
        async with pb_client.connect(endpoint, autostart=False) as client:
            await client.connect_hub(name=FIRST)

        # No clients attached now, and well past the idle timeout.
        await anyio.sleep(1.2)
        assert service.hubs.count == 1, "the hub should still be connected"
        assert service.shutdown_reason == "not requested"


async def test_connecting_again_reports_the_hub_already_connected(
    service_at: Endpoint, hubs: list[FakeHub]
) -> None:
    # A connected hub has stopped advertising, so a second connect that went
    # looking would find nothing and report that the hub must be switched off.
    async with pb_client.connect(service_at, autostart=False) as client:
        first = await client.connect_hub(name=hubs[0].name)
        again = await client.connect_hub()

    assert again.id == first.id
    assert again.state == "connected"


async def _settle() -> None:
    """Give the hub's status report time to reach the service.

    The service learns what is running from a status report, so a test that
    acted on the instant would be asking about a hub it had not heard from.
    """
    await anyio.sleep(0.1)


async def test_disconnecting_stops_a_built_in_program(
    service_at: Endpoint, hubs: list[FakeHub]
) -> None:
    # The REPL is a conversation with whoever is attached, and a hub only
    # advertises when nothing is running on it -- so leaving it going would
    # produce a hub that nothing could find afterwards.
    hub = hubs[0]

    async with pb_client.connect(service_at, autostart=False) as client:
        await client.connect_hub(name=hub.name)
        await client.start_repl()
        await _settle()
        assert hub.status & p.StatusFlag.USER_PROGRAM_RUNNING
        await client.disconnect_hub()

    assert not hub.status & p.StatusFlag.USER_PROGRAM_RUNNING


async def test_disconnecting_leaves_a_user_program_running(
    service_at: Endpoint, hubs: list[FakeHub]
) -> None:
    # Setting a program going and walking away is a thing people do on purpose.
    hub = hubs[0]
    hub.program_size = 8

    async with pb_client.connect(service_at, autostart=False) as client:
        await client.connect_hub(name=hub.name)
        await client.start_program()
        await _settle()
        assert hub.status & p.StatusFlag.USER_PROGRAM_RUNNING
        await client.disconnect_hub()

    assert hub.status & p.StatusFlag.USER_PROGRAM_RUNNING
