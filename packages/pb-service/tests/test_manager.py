# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What the service knows about which hubs it is holding, and when.

A hub can go away without being asked -- switched off, carried out of range --
and the registry used to find out by being swept four times a second. It is
told instead: the task holding a connection removes the entry when it ends, so
the two lifetimes are the same one. These tests are what stops that quietly
becoming a sweep again, or a leak.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from pathlib import Path

import anyio
import pytest
from anyio.lowlevel import checkpoint

from pb_service.hub.fake import FakeHub, FakeHubTransport
from pb_service.hub.manager import HubManager, HubNotFoundError, run_hub_manager
from pb_service.hub.selection import Selection
from pb_service.hub.transport import DeviceInfo, ScanFilter

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub()


@pytest.fixture
async def manager(
    hub: FakeHub, tmp_path: Path
) -> AsyncGenerator[tuple[HubManager, FakeHub], None]:
    transport = FakeHubTransport(hub)

    async with run_hub_manager(transport) as owner:
        # A choice of its own, so that resolution does not depend on
        # whatever this machine happens to have chosen.
        owner.selection = Selection(tmp_path / "selection.json")

        yield owner, hub


async def _find(manager: HubManager, device_id: str) -> DeviceInfo:
    """Scan until the wanted hub appears, then stop scanning.

    Closed in this task rather than left suspended: the scan holds the
    manager's lock, and a generator abandoned mid-iteration releases it
    from whichever task happens to finalise it.
    """

    with anyio.fail_after(5):
        async with aclosing(manager.discover(ScanFilter())) as stream:
            async for device in stream:
                if device.id == device_id:
                    return device

    raise AssertionError(f"never saw {device_id}")


async def _connected(manager: HubManager, hub: FakeHub) -> None:
    """Connect to the fake hub and check it is registered."""
    device = await _find(manager, hub.device_id)
    await manager.connect(device)

    assert manager.count == 1


async def _wait_until_forgotten(manager: HubManager, seconds: float) -> float:
    """How long the registry took to drop the hub.

    Args:
        manager: The manager to watch.
        seconds: How long to allow before giving up.

    Returns:
        Seconds waited.
    """
    began = anyio.current_time()

    with anyio.move_on_after(seconds):
        while manager.count:
            # A checkpoint rather than a period: what is being measured is how
            # soon the connection's own task gets to run, so this must not add
            # a wait of its own.
            await checkpoint()

    return anyio.current_time() - began


async def test_a_hub_that_goes_away_is_forgotten_without_being_swept(
    manager: tuple[HubManager, FakeHub],
) -> None:
    owner, hub = manager
    await _connected(owner, hub)

    # Switched off, or carried out of range: the transport notices and tells
    # the connection, and the connection unwinds.
    hub.disconnect()
    waited = await _wait_until_forgotten(owner, 5)

    assert owner.count == 0, "the hub is still registered as connected"
    # Nothing polls for this, so it happens as fast as the loop can run it.
    # The old sweep ran every 250 ms, so it could not have passed this.
    assert waited < 0.1, f"took {waited:.3f}s, which is a sweep rather than an event"

    with pytest.raises(HubNotFoundError):
        owner.get(None)


async def test_a_hub_asked_to_disconnect_is_gone_before_the_call_returns(
    manager: tuple[HubManager, FakeHub],
) -> None:
    # A client that asks to disconnect and then asks what is connected must
    # get the answer it just brought about, rather than one a moment behind.
    owner, hub = manager
    await _connected(owner, hub)

    await owner.disconnect(hub.device_id)

    assert owner.count == 0


async def test_the_fallback_hub_is_dropped_with_the_hub_it_names(
    manager: tuple[HubManager, FakeHub],
) -> None:
    # The most recently connected hub is what answers when nothing else says
    # which, and it is an id -- so it has to go when that hub does, or it
    # names something that is no longer there.
    owner, hub = manager
    await _connected(owner, hub)
    assert owner.get(None).id == hub.device_id

    hub.disconnect()
    await _wait_until_forgotten(owner, 5)

    with pytest.raises(HubNotFoundError):
        owner.get(None)
