# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Hub connections, fan-out and the buffering rules.

Several of these are the design's promises written as assertions rather than
prose: that a hub outlives the client that attached to it, that a client which
stops reading loses only its own events, and that a disconnect cancels
operations rather than leaving them hanging.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress

import anyio
import anyio.lowlevel
import pytest
from anyio.abc import TaskGroup

from pb_service.hub import protocol as p
from pb_service.hub.connection import (
    INGRESS_HIGH_WATER,
    SUBSCRIBER_QUEUE,
    HubCommandError,
    HubConnection,
    HubDisconnectedError,
    HubInfo,
)
from pb_service.hub.events import Channel, HubState, StatusEvent, StdoutEvent
from pb_service.hub.fake import FakeHub, FakeHubTransport, HubRejection
from pb_service.hub.transport import DeviceInfo, ScanFilter

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
) -> tuple[HubConnection, HubInfo]:
    """Connect to a fake hub, with the task owned by ``tg``."""
    device = await _find(transport, hub.device_id)
    connection = HubConnection(transport, device)
    info = await tg.start(connection.run)
    assert isinstance(info, HubInfo)
    return connection, info


@pytest.fixture
async def connected(
    hub: FakeHub,
) -> AsyncGenerator[tuple[HubConnection, FakeHub], None]:
    transport = FakeHubTransport(hub)
    async with anyio.create_task_group() as tg:
        connection, _ = await _start(tg, transport, hub)
        try:
            yield connection, hub
        finally:
            connection.close("test finished")


# ------------------------------------------------------------------ handshake


async def test_handshake_reads_identity_and_capabilities(hub: FakeHub) -> None:
    transport = FakeHubTransport(hub)
    async with anyio.create_task_group() as tg:
        connection, info = await _start(tg, transport, hub)
        assert info.name == hub.name
        assert info.firmware == hub.firmware
        assert info.profile == hub.profile
        assert info.capabilities.max_write_size == hub.max_write_size
        assert info.capabilities.num_slots == hub.num_slots
        assert connection.state is HubState.CONNECTED
        connection.close("done")


async def test_old_firmware_is_refused_with_advice() -> None:
    # A hub too old for the capabilities characteristic cannot be supported,
    # and the message should say what to do about it.
    hub = FakeHub(profile=(1, 1, 0))
    transport = FakeHubTransport(hub)
    with pytest.raises(BaseExceptionGroup) as info:
        async with anyio.create_task_group() as tg:
            await _start(tg, transport, hub)
    assert info.group_contains(HubCommandError, match="update the hub's firmware")


async def test_connect_failure_reaches_the_caller() -> None:
    # start() must surface handshake errors, or a caller would believe it had
    # connected to a hub that is not there.
    transport = FakeHubTransport(FakeHub())
    missing = DeviceInfo(id="fake:NO:SUCH", name="ghost", address="NO:SUCH")
    connection = HubConnection(transport, missing)
    with pytest.raises(BaseExceptionGroup) as info:
        async with anyio.create_task_group() as tg:
            await tg.start(connection.run)
    assert info.group_contains(ConnectionRefusedError)


# -------------------------------------------------------------------- fan-out


async def test_stdout_reaches_a_subscriber(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    connection, hub = connected
    async with connection.subscribe(frozenset({Channel.STDOUT})) as subscriber:
        hub.send_stdout(b"hello\n")
        with anyio.fail_after(5):
            event = await subscriber.rx.receive()
        assert isinstance(event, StdoutEvent)
        assert event.data == b"hello\n"


async def test_two_clients_receive_identical_output(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    # Sharing one hub between clients is the point of the daemon.
    connection, hub = connected
    async with (
        connection.subscribe(frozenset({Channel.STDOUT})) as first,
        connection.subscribe(frozenset({Channel.STDOUT})) as second,
    ):
        for index in range(5):
            hub.send_stdout(f"line {index}\n".encode())
        with anyio.fail_after(5):
            got_first = [(await first.rx.receive()) for _ in range(5)]
            got_second = [(await second.rx.receive()) for _ in range(5)]
    assert [e.data for e in got_first if isinstance(e, StdoutEvent)] == [
        e.data for e in got_second if isinstance(e, StdoutEvent)
    ]


async def test_detaching_one_client_leaves_the_hub_and_others_alone(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    # The promise that a hub survives its clients, as an assertion.
    connection, hub = connected
    async with connection.subscribe(frozenset({Channel.STDOUT})) as survivor:
        async with connection.subscribe(frozenset({Channel.STDOUT})):
            assert connection.subscriber_count == 2
        assert connection.subscriber_count == 1
        assert connection.state is HubState.CONNECTED

        hub.send_stdout(b"still here\n")
        with anyio.fail_after(5):
            event = await survivor.rx.receive()
        assert isinstance(event, StdoutEvent)


async def test_channels_are_independent(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    connection, hub = connected
    async with connection.subscribe(frozenset({Channel.STATUS})) as subscriber:
        hub.send_stdout(b"ignored by this subscriber\n")
        hub.set_program_running(True)
        with anyio.fail_after(5):
            event = await subscriber.rx.receive()
        assert isinstance(event, StatusEvent)
        assert event.report.program_running


async def test_a_slow_client_loses_only_its_own_events(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    # The load-bearing property: a client that stops reading must not be able
    # to slow the hub or affect anyone else.
    connection, hub = connected
    async with (
        connection.subscribe(frozenset({Channel.STDOUT})) as slow,
        connection.subscribe(frozenset({Channel.STDOUT})) as attentive,
    ):
        for index in range(SUBSCRIBER_QUEUE + 200):
            hub.send_stdout(str(index).encode())

        # Let the pump drain the ingress queue and fan everything out. The
        # attentive client keeps reading; the slow one never does.
        with anyio.fail_after(5):
            while slow.dropped == 0:
                with suppress(anyio.WouldBlock):
                    attentive.rx.receive_nowait()
                await anyio.sleep(0.01)

        assert slow.dropped > 0, "the idle client should have lost events"
        # Nothing was lost on the way in from the hub, only on the way out to
        # the client that stopped reading.
        assert connection.ingress_dropped == 0


async def test_program_output_is_shed_before_status_when_overloaded(
    hub: FakeHub,
) -> None:
    # Losing a status report would leave the daemon disagreeing with the hub
    # about whether a program is running, so output is shed first.
    transport = FakeHubTransport(hub)
    async with anyio.create_task_group() as tg:
        connection, _ = await _start(tg, transport, hub)
        # Stall the pump by never letting it run, then flood past the mark.
        connection._rx_bytes = INGRESS_HIGH_WATER + 1  # pyright: ignore[reportPrivateUsage]
        before = connection.ingress_dropped
        hub.send_stdout(b"x" * 100)
        after_stdout = connection.ingress_dropped
        assert after_stdout > before, "program output should have been shed"

        # A status report is still accepted at exactly the same pressure.
        hub.send_status()
        assert connection.ingress_dropped == after_stdout, (
            "status reports must never be dropped"
        )
        connection.close("done")


# ------------------------------------------------------------------ scrollback


async def test_a_late_client_can_replay_recent_output(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    # Starting a program in one window and attaching in another is the normal
    # sequence with a persistent daemon; without replay the first lines are
    # always missed.
    connection, hub = connected
    hub.send_stdout(b"printed before anyone attached\n")
    await anyio.sleep(0.05)

    async with connection.subscribe(
        frozenset({Channel.STDOUT}), replay_bytes=4096
    ) as subscriber:
        events = [event async for event in _take(connection, subscriber, 1)]
    assert isinstance(events[0], StdoutEvent)
    assert events[0].data == b"printed before anyone attached\n"


async def _take(
    connection: HubConnection, subscriber: object, count: int
) -> AsyncGenerator[object, None]:
    taken = 0
    async for event in connection.events(subscriber):  # pyright: ignore[reportArgumentType]
        yield event
        taken += 1
        if taken >= count:
            return


# ------------------------------------------------------------------- commands


async def test_commands_reach_the_hub(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    connection, hub = connected
    await connection.send_command(p.encode_stop_user_program())
    assert hub.commands[-1] == bytes([p.Command.STOP_USER_PROGRAM])


async def test_a_rejected_command_reports_the_firmware_error(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    connection, hub = connected
    hub.reject_next = p.HubError.BUSY
    with pytest.raises(HubRejection) as info:
        await connection.send_command(p.encode_stop_user_program())
    assert info.value.error is p.HubError.BUSY


async def test_commands_after_disconnect_are_refused(hub: FakeHub) -> None:
    transport = FakeHubTransport(hub)
    async with anyio.create_task_group() as tg:
        connection, _ = await _start(tg, transport, hub)
        connection.close("test")
    with pytest.raises(HubDisconnectedError):
        await connection.send_command(p.encode_stop_user_program())


# --------------------------------------------------------------- cancellation


async def test_disconnect_aborts_operations_in_flight(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    # Replaces racing an awaitable against a disconnect event and cancelling
    # the loser by hand: the scope does it, and cannot leak a task.
    connection, hub = connected
    reached_end = False

    with pytest.raises(HubDisconnectedError):
        async with connection.abort_on_disconnect():
            hub.disconnect()
            connection.notify_disconnected()
            await anyio.sleep(30)
            reached_end = True

    assert not reached_end


async def test_abort_scope_is_transparent_when_nothing_goes_wrong(
    connected: tuple[HubConnection, FakeHub],
) -> None:
    connection, _ = connected
    async with connection.abort_on_disconnect():
        await anyio.lowlevel.checkpoint()
    # No exception: the guard must not disturb the ordinary path.
