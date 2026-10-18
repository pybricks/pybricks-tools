# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Finding hubs over Bluetooth Classic, and failing to reach them.

The failing half is the point. A paired hub that is switched off is listed
exactly like one that is on -- the listing is the paired set, not a scan -- so
connecting to something that is not there is routine here rather than an edge
case, and has to arrive as a refusal rather than as a crash.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import aclosing

import anyio
import pytest

from pb_service.hub import protocol as p
from pb_service.hub.rfcomm.fake import FakePairedHub, radio, switch
from pb_service.hub.rfcomm.port import PYBRICKS_RFCOMM_SERVICE, SERIAL_PORT_SERVICE
from pb_service.hub.rfcomm.transport import POLL_SECONDS, RfcommHubTransport
from pb_service.hub.transport import DeviceInfo, ScanFilter
from pb_service.hub.usb.fake import FakeHubPort

pytestmark = pytest.mark.anyio

EV3 = "00:16:53:4A:75:15"
NXT = "00:16:53:0D:5E:AF"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


async def found(
    transport: RfcommHubTransport, scan_filter: ScanFilter | None = None
) -> list[DeviceInfo]:
    """Everything one pass of discovery reports."""
    seen: list[DeviceInfo] = []

    # One pass yields straight away and then waits out the poll, so a short
    # window collects exactly that pass.
    with anyio.move_on_after(0.1):
        async with aclosing(transport.discover(scan_filter or ScanFilter())) as hubs:
            async for hub in hubs:
                seen.append(hub)

    return seen


async def test_a_paired_hub_is_reported_with_its_type() -> None:
    transport = RfcommHubTransport(radio(FakePairedHub(address=EV3)))

    (hub,) = await found(transport)

    assert hub.id == f"rfcomm:{EV3}"
    assert hub.address == EV3
    assert hub.transport == "rfcomm"
    assert hub.hub_type == "EV3 brick"


async def test_the_type_suffix_is_not_part_of_the_name() -> None:
    # The listing has a column for the type, and the name has to match what
    # the hub answers with once connected, since a choice is stored by name.
    transport = RfcommHubTransport(
        radio(FakePairedHub(name="Bumblebee (MINDSTORMS EV3)", address=EV3))
    )

    (hub,) = await found(transport)

    assert hub.name == "Bumblebee"


async def test_a_serial_device_that_is_not_a_hub_is_left_out() -> None:
    # Every paired headset offers Serial Port, so the name is what separates a
    # brick from the rest of somebody's Bluetooth devices.
    transport = RfcommHubTransport(
        radio(
            FakePairedHub(
                name="Bose NC 700 Head",
                address="4C:87:5D:29:FA:5C",
                service=SERIAL_PORT_SERVICE,
            )
        )
    )

    assert await found(transport) == []


async def test_an_nxt_on_plain_serial_port_is_still_a_hub() -> None:
    transport = RfcommHubTransport(
        radio(
            FakePairedHub(
                name="Pybricks (MINDSTORMS NXT)",
                address=NXT,
                service=SERIAL_PORT_SERVICE,
            )
        )
    )

    (hub,) = await found(transport)

    assert hub.name == "Pybricks"
    assert hub.hub_type == "NXT brick"


async def test_a_hub_answering_on_both_services_is_reported_once() -> None:
    # A real EV3 does: its record declares Serial Port as well as the Pybricks
    # class, so both listings return it.
    both = [
        FakePairedHub(address=EV3, service=PYBRICKS_RFCOMM_SERVICE),
        FakePairedHub(address=EV3, service=SERIAL_PORT_SERVICE),
    ]
    transport = RfcommHubTransport(radio(*both))

    assert len(await found(transport)) == 1


async def test_asking_for_another_transport_reports_nothing() -> None:
    transport = RfcommHubTransport(radio(FakePairedHub(address=EV3)))

    assert await found(transport, ScanFilter(transport="usb")) == []


async def test_an_excluded_hub_is_not_reported() -> None:
    transport = RfcommHubTransport(radio(FakePairedHub(address=EV3)))

    excluded = ScanFilter(exclude=frozenset({f"rfcomm:{EV3}"}))

    assert await found(transport, excluded) == []


async def test_a_name_filter_matches_the_trimmed_name() -> None:
    transport = RfcommHubTransport(
        radio(FakePairedHub(name="Bumblebee (MINDSTORMS EV3)", address=EV3))
    )

    assert len(await found(transport, ScanFilter(name="Bumblebee"))) == 1
    assert await found(transport, ScanFilter(name="Bumblebee (MINDSTORMS EV3)")) == []


async def test_bluetooth_that_cannot_be_used_is_reported_rather_than_raised() -> None:
    # One transport's problem must not stop the others answering.
    transport = RfcommHubTransport(radio(unavailable="Bluetooth is turned off"))

    assert await found(transport) == []
    assert [(u.reason, u.kind) for u in transport.unavailable] == [
        ("Bluetooth is turned off", "adapter")
    ]


async def test_bluetooth_switched_off_and_on_mid_scan_is_noticed_at_once() -> None:
    # Well within the poll: a scan that ends just after the switch must
    # already say why, and stop saying it, and list the hub again, as soon as
    # that is untrue.
    bluetooth = radio(FakePairedHub(address=EV3))
    transport = RfcommHubTransport(bluetooth)
    seen: list[DeviceInfo] = []

    async def scan() -> None:
        # Pulling continuously, as the manager does: discovery only moves on
        # while someone is listening.
        async with aclosing(transport.discover(ScanFilter())) as hubs:
            async for hub in hubs:
                seen.append(hub)

    async def until(condition: Callable[[], object]) -> None:
        with anyio.fail_after(POLL_SECONDS / 4):
            # A poll rather than an event: what changes is the transport's own
            # state, which has nothing to signal with.
            while not condition():  # noqa: ASYNC110
                await anyio.sleep(0.005)

    async with anyio.create_task_group() as tg:
        tg.start_soon(scan)
        await until(lambda: seen)

        switch(bluetooth, "Bluetooth is turned off")
        await until(lambda: transport.unavailable)
        assert [u.reason for u in transport.unavailable] == ["Bluetooth is turned off"]

        seen.clear()
        switch(bluetooth, None)
        await until(lambda: seen)
        assert transport.unavailable == ()

        tg.cancel_scope.cancel()


async def test_a_hub_that_is_switched_off_refuses_rather_than_crashing() -> None:
    # The whole reason this file exists. A paired hub is listed whether or not
    # it is switched on, so this is the ordinary way connecting fails, and it
    # has to arrive as ConnectionError like every other transport reports.
    transport = RfcommHubTransport(radio(FakePairedHub(address=EV3, reachable=False)))

    (hub,) = await found(transport)

    with pytest.raises(ConnectionError):
        async with transport.open(hub.id):
            pass


async def test_a_hub_that_was_never_discovered_refuses() -> None:
    transport = RfcommHubTransport(radio())

    with pytest.raises(ConnectionError):
        async with transport.open("rfcomm:00:00:00:00:00:00"):
            pass


async def test_an_id_from_another_transport_refuses() -> None:
    transport = RfcommHubTransport(radio())

    with pytest.raises(ConnectionError):
        async with transport.open("usb:12345"):
            pass


async def test_a_hub_gone_part_way_through_a_handshake_refuses_plainly() -> None:
    # Review of the port found this: the channel dropping before the handshake
    # finished came out of the link's task group as an ExceptionGroup, which
    # connecting reported as an internal error rather than an unreachable hub.
    port = FakeHubPort(name="Bumblebee")
    transport = RfcommHubTransport(radio(FakePairedHub(address=EV3, port=port)))

    (hub,) = await found(transport)

    with pytest.raises(ConnectionError):
        async with transport.open(hub.id) as link:
            port.vanish()

            with anyio.fail_after(5):
                await link.read_characteristic(p.DEVICE_NAME_UUID)


async def test_an_open_hub_answers_the_protocol() -> None:
    port = FakeHubPort(name="Bumblebee")
    transport = RfcommHubTransport(radio(FakePairedHub(address=EV3, port=port)))

    (hub,) = await found(transport)

    async with transport.open(hub.id) as link:
        with anyio.fail_after(5):
            name = await link.read_characteristic(p.DEVICE_NAME_UUID)

    assert name == b"Bumblebee"
