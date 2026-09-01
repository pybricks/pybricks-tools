# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Writing to a hub's standard input, and what a refusal means.

The hub answers ``BUSY`` both when a write is larger than its input buffer and
when the buffer merely happens to be full at that moment, and nothing on the
wire tells the two apart. One is a property of the firmware and worth
remembering; the other lasts until the program on the hub reads a few bytes.
Confusing them is expensive in one direction, so both are tested here.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing

import anyio
import pytest
from anyio.abc import TaskGroup

from pb_service.hub.connection import HubConnection, HubInfo
from pb_service.hub.fake import FakeHub, FakeHubTransport
from pb_service.hub.transport import DeviceInfo, ScanFilter
from pb_service.methods import stdio
from pb_service.methods.stdio import (
    STDIN_FLOOR,
    # A test may reach into the module it is testing; nothing else may.
    _write_stdin,  # pyright: ignore[reportPrivateUsage]
)

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub()


async def _find(transport: FakeHubTransport, device_id: str) -> DeviceInfo:
    """Scan until the wanted hub appears, then stop scanning."""

    with anyio.fail_after(5):
        async with aclosing(transport.discover(ScanFilter())) as stream:
            async for device in stream:
                if device.id == device_id:
                    return device

    raise AssertionError(f"never saw {device_id}")


async def _start(
    tg: TaskGroup, transport: FakeHubTransport, hub: FakeHub
) -> HubConnection:
    """Connect to a fake hub, with the task owned by ``tg``."""
    device = await _find(transport, hub.device_id)
    connection = HubConnection(transport, device)
    info = await tg.start(connection.run)
    assert isinstance(info, HubInfo)

    return connection


@pytest.fixture
async def connected(
    hub: FakeHub,
) -> AsyncGenerator[tuple[HubConnection, FakeHub], None]:
    transport = FakeHubTransport(hub)

    async with anyio.create_task_group() as tg:
        connection = await _start(tg, transport, hub)

        try:
            yield connection, hub

        finally:
            connection.close("test finished")


async def _reading(hub: FakeHub, *, after: float = 0.05) -> None:
    """Stand for a program that gets round to reading its input."""
    await anyio.sleep(after)
    hub.stdin.clear()


async def test_a_hub_with_a_small_buffer_is_measured_once(
    connected: tuple[HubConnection, FakeHub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, hub = connected
    # The real quarter second is what makes the measurement trustworthy, and
    # there is nothing to be learnt from waiting it out five times here.
    monkeypatch.setattr(stdio, "STDIN_SETTLE_SECONDS", 0.01)
    # Firmware from before the input buffer grew. Nothing on the wire says so,
    # so the only way to find out is to be refused.
    hub.stdin_capacity = 21
    # Bound before the block: a task group may swallow what its body
    # raised, so nothing assigned inside one is certain to exist after it.
    written = 0

    async with anyio.create_task_group() as tg:
        tg.start_soon(_keep_draining, hub)
        written = await _write_stdin(connection, b"x" * 60)
        tg.cancel_scope.cancel()

    assert written == 60
    assert bytes(hub.stdin_writes[0]) != b"x" * 60
    assert max(len(write) for write in hub.stdin_writes) <= 21
    # And it is remembered, so the next keystroke does not pay for it again.
    assert connection.stdin_limit <= 21


async def _keep_draining(hub: FakeHub) -> None:
    """Stand for a program reading its input as it arrives."""

    while True:
        await anyio.sleep(0.005)
        hub.stdin.clear()


async def test_a_hub_that_is_briefly_full_is_not_measured_at_all(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    connection, hub = connected
    was = connection.stdin_limit
    # A buffer with plenty of room in it, but nearly full at this moment
    # because the program has not read yet. This is what pasting into the
    # interpreter looks like: the write after the first one meets whatever the
    # one before it left behind.
    hub.stdin_capacity = 128
    hub.stdin += b"y" * 120
    written = 0

    async with anyio.create_task_group() as tg:
        tg.start_soon(_reading, hub)
        written = await _write_stdin(connection, b"x" * 100)

    assert written == 100
    assert bytes(hub.stdin_writes[-1]) == b"x" * 100, (
        "the write should have waited for room rather than being cut up"
    )
    assert connection.stdin_limit == was, (
        f"a hub that was busy for a moment was mistaken for a small one: "
        f"the write size fell from {was} to {connection.stdin_limit} and "
        f"nothing puts it back"
    )
    assert connection.stdin_limit > STDIN_FLOOR
