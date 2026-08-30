# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Hub commands through the whole stack, against a simulated hub.

The client library, JSON-RPC, transport, daemon and hub logic are all real
here; only the radio is simulated. That means these run unchanged on a CI
machine with no Bluetooth adapter.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import anyio
import pytest

import pb_client
from pb_core.ipc import Endpoint
from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pbd import daemon as daemon_module
from pbd import (  # noqa: F401  (imported to register the RPC methods)
    methods_daemon,  # pyright: ignore[reportUnusedImport]
    methods_hub,  # pyright: ignore[reportUnusedImport]
    methods_program,  # pyright: ignore[reportUnusedImport]
)
from pbd.hub.fake import FakeHub, FakeHubTransport

pytestmark = pytest.mark.anyio

FIRST = "Pybricks Hub"
SECOND = "Spare Hub"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _private_runtime(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("PB_ENDPOINT", raising=False)
    yield


@pytest.fixture
def endpoint(tmp_path: Path) -> Endpoint:
    unique = uuid.uuid4().hex[:12]
    if sys.platform == "win32":
        return Endpoint("pipe", f"pb-hub-{unique}")
    return Endpoint("unix", str(tmp_path / f"{unique}.sock"))


@pytest.fixture
def hubs() -> list[FakeHub]:
    return [
        FakeHub(name=FIRST, address="00:11:22:33:44:55"),
        FakeHub(name=SECOND, address="66:77:88:99:AA:BB"),
    ]


@pytest.fixture
async def daemon_at(
    endpoint: Endpoint, hubs: list[FakeHub]
) -> AsyncGenerator[Endpoint, None]:
    transport = FakeHubTransport(*hubs)
    async with daemon_module.run_daemon(endpoint, idle_timeout=0, transport=transport):
        yield endpoint


async def test_scan_finds_the_simulated_hubs(daemon_at: Endpoint) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        devices = await daemon.scan(seconds=0.3)
    assert {device.name for device in devices} == {FIRST, SECOND}


async def test_scan_can_filter_by_name(daemon_at: Endpoint) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        devices = await daemon.scan(seconds=0.3, name=SECOND)
    assert [device.name for device in devices] == [SECOND]


async def test_scanning_reports_a_hub_long_before_the_scan_ends(
    daemon_at: Endpoint,
) -> None:
    # The whole point of the streaming form: a hub is in hand as soon as it is
    # heard. The deadline here is a hundredth of the scan it asks for, so this
    # cannot pass by accident of waiting it out.
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        with anyio.fail_after(5):
            async with daemon.scanning(seconds=500) as devices:
                first = await anext(aiter(devices))
    assert first.name in {FIRST, SECOND}
    assert first.rssi is not None
    # Known without connecting: the hub advertises its Plug and Play ID in the
    # scan response, which is what lets a scan tell two hubs of different kinds
    # apart when they have the same name.
    assert first.hub_type == "Prime hub"


async def test_scanning_ends_by_itself_when_the_scan_is_over(
    daemon_at: Endpoint,
) -> None:
    # Nothing tells the caller's loop to stop except the scan finishing, so if
    # that signal is missing this hangs rather than fails -- hence the deadline.
    names: set[str] = set()
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        with anyio.fail_after(10):
            async with daemon.scanning(seconds=0.5) as devices:
                async for device in devices:
                    names.add(device.name)
    assert names == {FIRST, SECOND}


async def test_a_scan_that_reports_nothing_still_ends(daemon_at: Endpoint) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        with anyio.fail_after(10):
            async with daemon.scanning(seconds=0.3, name="No Such Hub") as devices:
                assert [device async for device in devices] == []


async def test_connect_and_describe(daemon_at: Endpoint) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        info = await daemon.connect_hub(name=FIRST)
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
        daemon_module.run_daemon(
            endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
        ),
        pb_client.connect(endpoint, autostart=False) as daemon,
    ):
        info = await daemon.connect_hub(name=FIRST)
    assert info.hub_type is None
    assert info.state == "connected"


async def test_a_hub_outlives_the_client_that_connected_it(
    daemon_at: Endpoint,
) -> None:
    # The premise of the design, at the level a user sees it: connect in one
    # command, and a completely separate command still finds it connected.
    async with pb_client.connect(daemon_at, autostart=False) as first:
        await first.connect_hub(name=FIRST)

    async with pb_client.connect(daemon_at, autostart=False) as second:
        hubs, default = await second.list_hubs()

    assert [hub.name for hub in hubs] == [FIRST]
    assert default == hubs[0].id


async def test_connecting_twice_reuses_the_connection(daemon_at: Endpoint) -> None:
    # Reconnecting over Bluetooth is slow, so a second request for a hub we
    # already hold must not start a new connection.
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        first = await daemon.connect_hub(name=FIRST)
        second = await daemon.connect_hub(name=FIRST)
        assert first.id == second.id
        hubs, _ = await daemon.list_hubs()
        assert len(hubs) == 1


async def test_several_hubs_at_once(daemon_at: Endpoint) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        await daemon.connect_hub(name=FIRST)
        await daemon.connect_hub(name=SECOND)
        hubs, default = await daemon.list_hubs()
    assert {hub.name for hub in hubs} == {FIRST, SECOND}
    # The most recently connected hub becomes the default.
    assert default == next(hub.id for hub in hubs if hub.name == SECOND)


async def test_scan_hides_hubs_that_are_already_connected(
    daemon_at: Endpoint,
) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        await daemon.connect_hub(name=FIRST)
        devices = await daemon.scan(seconds=0.3)
    assert [device.name for device in devices] == [SECOND]


async def test_choosing_the_default_hub(daemon_at: Endpoint) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        first = await daemon.connect_hub(name=FIRST)
        await daemon.connect_hub(name=SECOND)

        chosen = await daemon.use_hub(first.id)
        assert chosen == first.id

        # A command with no hub named now acts on that one.
        info = await daemon.hub_info()
        assert info.name == FIRST


async def test_a_hub_can_be_named_by_name(daemon_at: Endpoint) -> None:
    # A name is all "pb --hub" ever passes, because a hub id is built from the
    # Bluetooth address, which is not stable and is not shown to anyone. Every
    # method that takes a hub has to accept one.
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        await daemon.connect_hub(name=FIRST)
        await daemon.connect_hub(name=SECOND)

        info = await daemon.hub_info(FIRST)
        assert info.name == FIRST

        # Not merely the default: SECOND was connected last and holds it.
        await daemon.disconnect_hub(FIRST)
        hubs, _ = await daemon.list_hubs()
        assert [hub.name for hub in hubs] == [SECOND]


async def test_disconnect_removes_the_hub(daemon_at: Endpoint) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        info = await daemon.connect_hub(name=FIRST)
        await daemon.disconnect_hub(info.id)
        hubs, _ = await daemon.list_hubs()
    assert hubs == []


async def test_commands_for_an_unknown_hub_are_reported(daemon_at: Endpoint) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        with pytest.raises(JsonRpcError) as info:
            await daemon.hub_info("fake:NO:SUCH:HUB")
        assert info.value.code == ErrorCode.HUB_NOT_FOUND


async def test_connecting_to_a_hub_that_is_not_there(daemon_at: Endpoint) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        with pytest.raises(JsonRpcError) as info:
            await daemon.connect_hub(name="No Such Hub")
        assert info.value.code == ErrorCode.HUB_NOT_FOUND
        assert info.value.message == "no hub found"


async def test_stop_program_reaches_the_hub(
    daemon_at: Endpoint, hubs: list[FakeHub]
) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        await daemon.connect_hub(name=FIRST)
        await daemon.stop_program()
    from pbd.hub import protocol as p

    assert hubs[0].commands[-1] == bytes([p.Command.STOP_USER_PROGRAM])


async def test_status_reflects_a_running_program(
    daemon_at: Endpoint, hubs: list[FakeHub]
) -> None:
    async with pb_client.connect(daemon_at, autostart=False) as daemon:
        await daemon.connect_hub(name=FIRST)
        hubs[0].set_program_running(True)

        with anyio.fail_after(5):
            while True:
                info = await daemon.hub_info()
                if info.program_running:
                    break
                await anyio.sleep(0.02)
        assert "USER_PROGRAM_RUNNING" in info.status_names


async def test_a_connected_hub_keeps_the_daemon_alive(
    endpoint: Endpoint, hubs: list[FakeHub]
) -> None:
    # Holding the connection open with nobody attached is the entire point, so
    # the idle timeout must not fire while a hub is connected.
    transport = FakeHubTransport(*hubs)
    async with daemon_module.run_daemon(
        endpoint, idle_timeout=0.4, transport=transport
    ) as daemon:
        async with pb_client.connect(endpoint, autostart=False) as client:
            await client.connect_hub(name=FIRST)

        # No clients attached now, and well past the idle timeout.
        await anyio.sleep(1.2)
        assert daemon.hubs.count == 1, "the hub should still be connected"
        assert daemon.shutdown_reason == "not requested"
