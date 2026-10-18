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
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import aio_rfcomm
import anyio
import pytest

import pb_client
import pb_service.methods.hub  # pyright: ignore[reportUnusedImport]
import pb_service.methods.program  # pyright: ignore[reportUnusedImport]

# Imported to register the RPC methods.
import pb_service.methods.service  # pyright: ignore[reportUnusedImport]
import pb_service.methods.stdio  # pyright: ignore[reportUnusedImport]
from pb_core.ipc import Endpoint
from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_service import service as service_module
from pb_service.hub import protocol as p
from pb_service.hub.fake import FakeHub, FakeHubTransport
from pb_service.hub.rfcomm.fake import FakePairedHub, radio, switch
from pb_service.hub.rfcomm.transport import RfcommHubTransport
from pb_service.hub.transport import UnavailableDevice

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


async def test_connecting_to_a_hub_that_is_not_there(service_at: Endpoint) -> None:
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
