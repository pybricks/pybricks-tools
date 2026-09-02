# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Talking to a hub over a serial port, when the hub does not cooperate.

The framing is covered against frames captured from real hardware, and the
transports above it against stubs. What was left is the part in between -- the
link itself -- and specifically what it does when a hub refuses a command,
answers nothing, sends a frame that will not decode, or goes away in the middle
of a request. None of those can be asked of a hub on a cable, which is why they
had no tests until there was a port that could be told to misbehave.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncGenerator

import anyio
import pytest

from pb_service.hub import protocol as p
from pb_service.hub.transport import HubCommandError, HubLink, ScanFilter
from pb_service.hub.usb import wire
from pb_service.hub.usb.fake import FakeHubPort, lister, opener
from pb_service.hub.usb.transport import UsbHubTransport

pytestmark = pytest.mark.anyio

STOP: bytes = bytes((p.Command.STOP_USER_PROGRAM,))
NAME_SELECTOR = wire.READ_SELECTORS[p.DEVICE_NAME_UUID]


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


def transport_for(*ports: FakeHubPort) -> UsbHubTransport:
    """Build a transport whose serial ports are these hubs.

    Args:
        *ports: The hubs plugged in.

    Returns:
        The real transport, which cannot tell the difference.
    """
    return UsbHubTransport(lister(*ports), opener(*ports))


async def _connected(port: FakeHubPort) -> AsyncGenerator[HubLink, None]:
    """Open a link to one hub, and give the port back afterwards."""
    try:
        transport = transport_for(port)

        async with transport.open(f"usb:{port.info.serial_number}") as link:
            yield link
    finally:
        port.close()


@pytest.fixture
async def hub() -> AsyncGenerator[FakeHubPort, None]:
    port = FakeHubPort()

    try:
        yield port
    finally:
        port.close()


@pytest.fixture
async def link(hub: FakeHubPort) -> AsyncGenerator[HubLink, None]:
    transport = transport_for(hub)

    async with transport.open(f"usb:{hub.info.serial_number}") as opened:
        yield opened


# ----------------------------------------------------------- the ordinary way


async def test_opening_subscribes(link: HubLink, hub: FakeHubPort) -> None:
    # Subscribing is what makes the hub treat us as its host, so it is what
    # stops it advertising over Bluetooth and stops it switching itself off.
    # Opening the port is not enough, and only the hub can say so.
    await link.start_notify(p.PYBRICKS_COMMAND_EVENT_UUID, lambda _: None)

    assert hub.subscribed


async def test_a_characteristic_reads_back(link: HubLink) -> None:
    assert await link.read_characteristic(p.DEVICE_NAME_UUID) == b"Bumblebee"


async def test_a_command_the_hub_accepts_returns(
    link: HubLink, hub: FakeHubPort
) -> None:
    await link.write_characteristic(p.PYBRICKS_COMMAND_EVENT_UUID, STOP)

    assert hub.commands == [STOP]


async def test_events_reach_the_handler(link: HubLink, hub: FakeHubPort) -> None:
    seen: list[bytes] = []
    await link.start_notify(p.PYBRICKS_COMMAND_EVENT_UUID, seen.append)

    hub.send_status(1 << 3)

    # A read is a round trip, so the event queued before it has been through
    # the reader by the time the answer arrives. Nothing to sleep for.
    await link.read_characteristic(p.DEVICE_NAME_UUID)

    assert seen == [bytes((p.Event.STATUS_REPORT,)) + struct.pack("<I", 1 << 3)]


async def test_an_event_arriving_mid_request_is_not_read_as_the_reply(
    link: HubLink, hub: FakeHubPort
) -> None:
    # Events interleave with replies on one stream, so the reply to a request
    # is never simply the next frame to arrive. Handing this event to the
    # waiting reader would answer "what is your name" with a status report.
    await link.start_notify(p.PYBRICKS_COMMAND_EVENT_UUID, lambda _: None)

    hub.send_status(1 << 3)

    assert await link.read_characteristic(p.DEVICE_NAME_UUID) == b"Bumblebee"


# ---------------------------------------------------------- when it goes wrong


async def test_a_refused_command_raises_the_hubs_own_error() -> None:
    # A refusal is a reply rather than a silence, and the code inside it is the
    # point: the CLI can say something exact because this preserves it.
    async for link in _connected(FakeHubPort(refuse_with=p.HubError.BUSY)):
        with pytest.raises(HubCommandError) as refusal:
            await link.write_characteristic(p.PYBRICKS_COMMAND_EVENT_UUID, STOP)

        assert refusal.value.error == p.HubError.BUSY


async def test_a_refusal_the_caller_asked_not_to_hear_is_still_collected() -> None:
    # `response=False` says the caller does not want the answer. The answer is
    # read anyway, because leaving it queued would make it the next request's.
    async for link in _connected(FakeHubPort(refuse_with=p.HubError.BUSY)):
        await link.write_characteristic(
            p.PYBRICKS_COMMAND_EVENT_UUID, STOP, response=False
        )

        assert await link.read_characteristic(p.DEVICE_NAME_UUID) == b"Bumblebee"


async def test_a_read_that_is_never_answered_gives_up() -> None:
    async for link in _connected(FakeHubPort(answer_reads=False)):
        with anyio.move_on_after(0.2) as scope:
            await link.read_characteristic(p.DEVICE_NAME_UUID)

        assert scope.cancelled_caught, "it answered for a hub that said nothing"


async def test_a_command_that_is_never_answered_gives_up() -> None:
    async for link in _connected(FakeHubPort(answer_commands=False)):
        with anyio.move_on_after(0.2) as scope:
            await link.write_characteristic(p.PYBRICKS_COMMAND_EVENT_UUID, STOP)

        assert scope.cancelled_caught


async def test_a_hub_that_goes_away_is_reported_once_and_refuses_more_work(
    hub: FakeHubPort,
) -> None:
    lost: list[str] = []
    gone = anyio.Event()

    def note(device_id: str) -> None:
        lost.append(device_id)
        gone.set()

    transport = transport_for(hub)
    transport.on_disconnect = note

    async with transport.open(f"usb:{hub.info.serial_number}") as link:
        assert link.is_connected

        hub.vanish()

        with anyio.fail_after(5):
            await gone.wait()

        # Waited on rather than looked for: the report is the event, and the
        # flag behind it is set on the way out of the reader's own task.
        with pytest.raises(ConnectionError):
            await link.write_characteristic(p.PYBRICKS_COMMAND_EVENT_UUID, STOP)

    assert lost == [f"usb:{hub.info.serial_number}"]


async def test_a_link_closed_on_request_is_not_reported_as_a_hub_going_away(
    hub: FakeHubPort,
) -> None:
    # The rule that telling somebody to go and rescue a hub has to be earned.
    # Leaving on purpose unwinds through the same reader, and reporting that as
    # a loss would send them across the room to a hub that is perfectly happy.
    lost: list[str] = []
    transport = transport_for(hub)
    transport.on_disconnect = lost.append

    async with transport.open(f"usb:{hub.info.serial_number}"):
        pass

    assert lost == []


async def test_a_read_in_flight_when_the_hub_vanishes_reports_a_disconnect() -> None:
    # The failure this whole file exists for. The reader's streams close when
    # the link ends, and a request waiting on them used to come out of anyio as
    # an EndOfStream -- an internal nobody above catches -- rather than as the
    # ConnectionError that a failed write already reports for the same event.
    silent = FakeHubPort(answer_reads=False)
    refused: list[ConnectionError] = []

    async def ask(link: HubLink) -> None:
        # Deliberately narrow: anything else propagates and fails the test
        # with its own name, which is more use than a broad catch would be.
        try:
            await link.read_characteristic(p.DEVICE_NAME_UUID)
        except ConnectionError as exc:
            refused.append(exc)

    async for link in _connected(silent):
        async with anyio.create_task_group() as tg:
            tg.start_soon(ask, link)

            with anyio.fail_after(5):
                await silent.asked.wait()

            silent.vanish()

    assert len(refused) == 1


async def test_an_undecodable_frame_costs_the_frame_and_not_the_alignment(
    link: HubLink, hub: FakeHubPort
) -> None:
    # A corrupt frame is discarded at the next delimiter, so the reply after it
    # still arrives. Losing alignment instead would break the link for good.
    hub.send_undecodable()

    assert await link.read_characteristic(p.DEVICE_NAME_UUID) == b"Bumblebee"
    assert link.is_connected


async def test_a_late_reply_is_not_the_next_requests_answer() -> None:
    # The trap a byte stream sets, and the reason a request drops what is
    # queued before it asks. A read that gave up leaves its answer behind, and
    # reading that as the next answer returns the firmware revision to somebody
    # who asked for the hub's name.
    slow = FakeHubPort(answer_reads=False)

    async for link in _connected(slow):
        with anyio.move_on_after(0.1):
            await link.read_characteristic(p.FIRMWARE_REVISION_UUID)

        # The abandoned read's answer turns up now, ahead of the next one.
        slow.send(
            wire.InMessage.READ_REPLY,
            struct.pack("<BH", wire.ReadService.GATT, 0x2A26) + b"4.1.0b3",
        )
        slow.send(
            wire.InMessage.READ_REPLY,
            struct.pack("<BH", *NAME_SELECTOR) + b"Bumblebee",
        )

        assert await link.read_characteristic(p.DEVICE_NAME_UUID) == b"Bumblebee"


async def test_a_characteristic_with_no_usb_equivalent_is_refused(
    link: HubLink,
) -> None:
    with pytest.raises(ValueError, match="no USB equivalent"):
        await link.read_characteristic("0000face-0000-1000-8000-00805f9b34fb")


# --------------------------------------------------------------- and discovery


async def test_discovery_names_a_hub_without_becoming_its_host(
    hub: FakeHubPort,
) -> None:
    # What lets a listing be a listing: reads work unsubscribed, so a scan can
    # name every hub plugged in without taking any of them, without stopping
    # them advertising, and without resetting the timer that switches an idle
    # hub off.
    transport = transport_for(hub)
    found = None

    with anyio.fail_after(5):
        async for device in transport.discover(ScanFilter()):
            found = device
            break

    assert found is not None
    assert (found.name, found.hub_type) == ("Bumblebee", "Inventor hub")
    assert not hub.subscribed, "naming a hub took it"


async def test_a_port_that_says_nothing_is_not_reported_as_a_hub() -> None:
    # What a serial port belonging to something else looks like -- and also
    # what a hub somebody else already has open looks like. Neither is offered.
    mute = FakeHubPort(answer_reads=False)

    try:
        transport = transport_for(mute)

        with anyio.move_on_after(0.5):
            async for device in transport.discover(ScanFilter()):
                raise AssertionError(f"offered {device.id}")
    finally:
        mute.close()


async def test_two_hubs_are_told_apart_by_their_serial_numbers() -> None:
    first = FakeHubPort(name="Bumblebee", device="COM5", serial_number="AAA")
    second = FakeHubPort(name="Rover", device="COM6", serial_number="BBB")

    try:
        transport = transport_for(first, second)
        found: dict[str, str] = {}

        with anyio.fail_after(5):
            async for device in transport.discover(ScanFilter()):
                found[device.id] = device.name

                if len(found) == 2:
                    break

        assert found == {"usb:AAA": "Bumblebee", "usb:BBB": "Rover"}
    finally:
        first.close()
        second.close()
