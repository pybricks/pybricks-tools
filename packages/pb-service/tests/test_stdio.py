# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Writing to a hub's standard input, and what a refusal means.

A refusal means one thing: nothing on the hub has read its input lately. The
buffer is emptied only by whatever is on the other end -- the interpreter while
it waits at a prompt, a user program as often as it reads ``sys.stdin`` -- so
``BUSY`` is a statement about the reader and not about the buffer's size.

So the write size never moves, and these tests are mostly about that: a hub
that is slow to read is waited for at full size, a hub that never reads is
given up on with an honest message, and neither leaves the connection changed.
An earlier version halved the size on a refusal that lasted and remembered
where it stopped, which cost a City hub its throughput for the life of the
connection after one program that ignored its input.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing

import anyio
import pytest
from anyio.abc import TaskGroup

from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_service.hub import protocol as p
from pb_service.hub.connection import HubConnection, HubInfo
from pb_service.hub.fake import FakeHub, FakeHubTransport
from pb_service.hub.transport import DeviceInfo, ScanFilter
from pb_service.methods import stdio
from pb_service.methods.stdio import (
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


async def test_a_hub_that_is_slow_to_read_is_waited_for_at_full_size(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    # What pasting into the interpreter looks like: the write after the first
    # meets whatever the first left behind, so it is refused until the hub
    # reads. It must be waited for whole rather than cut up, because cutting it
    # up is what used to make the connection slow for good.
    connection, hub = connected
    was = connection.stdin_limit
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
    assert connection.stdin_limit == was


async def test_a_hub_that_never_reads_is_given_up_on_and_left_alone(
    connected: tuple[HubConnection, FakeHub],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A program that ignores its input refuses every write for as long as it
    # runs. There is nothing to be done about that from here, so the caller is
    # told so -- and the connection is left exactly as it was found, which is
    # the part the earlier adaptive version got wrong.
    connection, hub = connected
    monkeypatch.setattr(stdio, "STDIN_PATIENCE_SECONDS", 0.3)
    was = connection.stdin_limit
    # Room left over, but never enough for the whole write and never growing.
    hub.stdin_capacity = 128
    hub.stdin += b"y" * 110

    with pytest.raises(JsonRpcError) as caught:
        await _write_stdin(connection, b"x" * 100)

    assert caught.value.code == ErrorCode.HUB_BUSY
    assert "not reading its input" in caught.value.message
    assert connection.stdin_limit == was, (
        f"the write size fell from {was} to {connection.stdin_limit}, and "
        f"nothing puts it back for the life of the connection"
    )
    # And nothing smaller was smuggled through in the meantime.
    assert not hub.stdin_writes


async def test_the_write_size_is_what_the_hub_said_and_nothing_else(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    connection, hub = connected
    info = connection.info
    assert info is not None
    assert connection.stdin_limit == (
        info.capabilities.max_write_size - p.WRITE_STDIN_HEADER_SIZE
    )

    # A refusal changes nothing about it, however long it lasts.
    was = connection.stdin_limit
    hub.stdin_capacity = 8
    hub.stdin += b"y" * 8

    with anyio.move_on_after(0.2):
        await _write_stdin(connection, b"x" * 100)

    assert connection.stdin_limit == was
