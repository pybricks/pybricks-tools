# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Getting a real hub in front of a test, or skipping cleanly.

These tests need a Pybricks hub switched on and in range, so they are marked
``hardware`` and deselected in CI with ``-m "not hardware"``. Run them
deliberately::

    uv run pytest -m hardware

**They use the service that is already running**, rather than starting a private
one, and that is not laziness. Only one Bluetooth central can hold a hub, so a
service of our own could not connect to any hub the developer's session is
already holding -- and the ambient service is what a user has, which makes it
the thing worth testing.

Everything here leaves the hub with nothing running. A Pybricks hub that is
running a program does not advertise, so a test that walks away from one leaves
a hub that nothing can find until somebody presses its button.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator

import anyio
import pytest

import pb_client

SCAN_SECONDS = 15.0
"""How long to look for a hub before deciding there is none to test against."""

_UNAVAILABLE = (
    pb_client.PbClientError,
    pb_client.JsonRpcError,
    TimeoutError,
    BaseExceptionGroup,
)
"""What to catch when deciding whether there is anything to test against.

Three things about this list are easy to get wrong, and leaving any of them out
turns "there is no hub here" into an error rather than a skip.

``JsonRpcError`` is **not** a ``PbClientError`` -- they are unrelated roots,
both straight off ``Exception`` -- and it is the one a machine with no
Bluetooth raises, since the failure happens on the service's side of the wire
and comes back as an error response. Measured on WSL, where there is no
adapter: ``[-32603] BleakDBusError: ... The name org.bluez was not provided by
any .service files``.

The group is not optional either: anything raised inside an ``async with
pb_client.connect()`` comes back wrapped in a group of one, so the type to
catch is not the type that was raised.
"""


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
async def _service_from_this_working_tree() -> AsyncGenerator[None, None]:  # pyright: ignore[reportUnusedFunction]
    """Stop any service left running, so a fresh one starts from this source.

    **Without this these tests measure a stale process**, and that is not a
    theoretical worry -- it was caught by mutation. The service outlives every
    command by design, so one started this morning is still running this
    afternoon with this morning's code loaded; editing the service and running
    the hardware tests then reports on the edit that is not there. Restoring
    the adaptive write size this suite exists to guard against, and running the
    guard, passed.

    It costs a reconnection at the start of a run, which is seconds. Any hub the
    old service was holding is dropped, and the service stops a built-in
    program before it lets go, so nothing is left running on a hub that would
    then stop advertising.

    Yields:
        Nothing. The service is left running, holding whatever was connected.
    """

    with contextlib.suppress(*_UNAVAILABLE):
        async with pb_client.connect(autostart=False) as running:
            await running.shutdown()

    # Long enough for it to let go of its lock and its socket or pipe, so that
    # the next connection starts a new one rather than failing to bind.
    await anyio.sleep(2.0)

    yield


@pytest.fixture
async def service() -> AsyncGenerator[pb_client.Service, None]:
    """The running service, or a skip if there is not one to be had.

    Yields:
        A client connected to it.
    """

    # Asked and answered before the connection the test will use, rather than
    # wrapping the ``yield`` in a ``try``: an exception from the test body
    # arrives at the yield, and a fixture that caught those would turn a
    # failure into a skip.
    try:
        async with pb_client.connect(autostart=True) as probe:
            await probe.ping()

    except _UNAVAILABLE as exc:
        pytest.skip(f"no service to test against: {exc}")

    async with pb_client.connect(autostart=False) as connected:
        yield connected


@pytest.fixture
async def hub(service: pb_client.Service) -> AsyncGenerator[str, None]:
    """A connected hub, by name, with nothing running on it.

    Args:
        service: The running service.

    Yields:
        The hub's name, which is what to pass as ``hub_id`` -- every RPC that
        takes a hub resolves a name as readily as an id.
    """
    name = await _connected(service)
    await _quiesce(service, name)

    yield name

    await _quiesce(service, name)


async def _connected(service: pb_client.Service) -> str:
    """Find a hub to test against, connecting to one if need be.

    Prefers a hub the service is already holding, because connecting takes
    seconds and because that hub is the one the developer is working with.

    Args:
        service: The running service.

    Returns:
        The hub's name.
    """
    hubs, _ = await service.list_hubs()

    if hubs:
        return hubs[0].name

    try:
        with anyio.fail_after(SCAN_SECONDS):
            info = await service.connect_hub()

    except _UNAVAILABLE as exc:
        pytest.skip(f"no hub switched on and in range: {exc}")

    return info.name


async def _quiesce(service: pb_client.Service, name: str) -> None:
    """Leave the hub running nothing at all.

    Args:
        service: The running service.
        name: Which hub.
    """
    # A hub with nothing running refuses this, which is the ordinary case here
    # rather than a failure: all that matters is what is true afterwards.
    with contextlib.suppress(*_UNAVAILABLE):
        await service.stop_program(hub_id=name)

    # Long enough for the status report saying so to come back, so that a test
    # does not read the state from before it was stopped.
    await anyio.sleep(0.3)
