# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Writing firmware to a Powered Up hub, against a simulated bootloader.

The bootloader speaks the real wire format, so what is exercised is the code
that will talk to hardware. What is simulated is the hub.

The properties worth asserting are not "it finished". They are that every byte
arrived, that the addresses were contiguous from the bootloader's own start
address -- firmware written to the wrong place still adds up to the right
number of bytes -- and that the two documented workarounds still work.
"""

from __future__ import annotations

from typing import final

import pytest

from pb_service.firmware.bootloader import lwp3, powered_up
from pb_service.firmware.errors import FirmwareIncompatibleError
from pb_service.firmware.fake import FakeBootloader
from pb_service.hub.transport import NotificationHandler

pytestmark = pytest.mark.anyio

MOVE_HUB = 0x40
CITY_HUB = 0x41


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def impatient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the waits, for the tests that are about waiting.

    The real numbers are chosen for hubs on a slow radio. A test that sat out
    five real seconds to prove a timeout works would be five seconds every
    time the suite runs, for a duration nothing depends on.
    """
    for name in ("REPLY_TIMEOUT", "ERASE_TIMEOUT", "CHECKSUM_TIMEOUT"):
        monkeypatch.setattr(powered_up, name, 0.05)


@final
class FakeLink:
    """A connection whose far end is a simulated bootloader."""

    def __init__(self, bootloader: FakeBootloader) -> None:
        self.bootloader = bootloader
        self.responses: list[bool] = []
        """Whether each write asked for a response, which one hub cares about."""

    async def read_characteristic(self, uuid: str) -> bytes:
        raise AssertionError(f"nothing reads {uuid} while flashing")

    async def write_characteristic(
        self, uuid: str, data: bytes, *, response: bool = True
    ) -> None:
        assert uuid == lwp3.BOOTLOADER_CHARACTERISTIC_UUID
        self.responses.append(response)
        self.bootloader.handle(data)

    async def start_notify(self, uuid: str, handler: NotificationHandler) -> None:
        assert uuid == lwp3.BOOTLOADER_CHARACTERISTIC_UUID
        self.bootloader._handler = handler  # pyright: ignore[reportPrivateUsage]

    @property
    def is_connected(self) -> bool:
        return True


async def test_the_whole_image_arrives_where_it_should() -> None:
    # Contiguous from the bootloader's own start address, because firmware
    # written to the wrong place still adds up to the right size.
    bootloader = FakeBootloader(hub_type=CITY_HUB)
    link = FakeLink(bootloader)
    image = bytes(range(256)) * 4

    await powered_up.flash(link, image, expect_hub_type=CITY_HUB)

    assert bytes(bootloader.written) == image
    assert bootloader.contiguous
    assert bootloader.erased
    assert bootloader.started
    assert bootloader.declared_size == len(image)


async def test_each_hub_is_written_in_its_own_size_of_chunk() -> None:
    # Fourteen bytes on a Move hub, whose Bluetooth maximum transmission unit
    # cannot be raised. Too large a write fails part way through a flash.
    bootloader = FakeBootloader(hub_type=MOVE_HUB)
    image = bytes(100)
    await powered_up.flash(FakeLink(bootloader), image, expect_hub_type=MOVE_HUB)

    assert bytes(bootloader.written) == image
    assert {length for _, length in bootloader.writes[:-1]} == {14}


async def test_progress_is_reported_as_it_is_written() -> None:
    bootloader = FakeBootloader(hub_type=CITY_HUB)
    image = bytes(200)
    seen: list[tuple[int, int]] = []

    await powered_up.flash(
        FakeLink(bootloader),
        image,
        expect_hub_type=CITY_HUB,
        on_progress=lambda done, total: seen.append((done, total)),
    )

    assert seen[-1] == (len(image), len(image))
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)


async def test_the_wrong_hub_is_refused_before_anything_is_erased() -> None:
    # Checked against what the device says it is, not against the
    # advertisement that led us to it: this is the thing about to be written.
    bootloader = FakeBootloader(hub_type=MOVE_HUB)
    with pytest.raises(FirmwareIncompatibleError, match="0x40"):
        await powered_up.flash(
            FakeLink(bootloader), bytes(64), expect_hub_type=CITY_HUB
        )

    assert not bootloader.erased
    assert not bootloader.written


async def test_a_city_hub_erase_is_written_with_a_response() -> None:
    # Its bootloader sends a write response even when told not to, which
    # confuses some Bluetooth stacks, so the write always asks for one.
    link = FakeLink(FakeBootloader(hub_type=CITY_HUB))
    await powered_up.flash(link, bytes(64), expect_hub_type=CITY_HUB)
    # The first write is the info request, the second the erase.
    assert link.responses[1] is True


async def test_another_hub_erase_is_written_without_one() -> None:
    link = FakeLink(FakeBootloader(hub_type=MOVE_HUB))
    await powered_up.flash(link, bytes(64), expect_hub_type=MOVE_HUB)
    assert link.responses[1] is False


async def test_an_erase_that_is_never_acknowledged_does_not_stop_the_flash(
    impatient: None,
) -> None:
    # Observed on real hubs: the reply sometimes never arrives, and waiting
    # for it forever would strand a flash that is in fact proceeding.
    bootloader = FakeBootloader(hub_type=CITY_HUB, answer_erase=False)
    image = bytes(64)

    await powered_up.flash(FakeLink(bootloader), image, expect_hub_type=CITY_HUB)

    assert bytes(bootloader.written) == image
    assert bootloader.started


async def test_a_hub_that_took_the_wrong_amount_is_not_started() -> None:
    # The one acknowledgement in the whole flash says how much arrived. If it
    # disagrees, what is on the hub is not what was sent, and starting it
    # would be the worst outcome available.
    bootloader = FakeBootloader(hub_type=CITY_HUB, report_count_as=64)

    with pytest.raises(FirmwareIncompatibleError, match="took 64 bytes"):
        await powered_up.flash(
            FakeLink(bootloader), bytes(200), expect_hub_type=CITY_HUB
        )

    assert not bootloader.started


async def test_a_bootloader_that_stops_answering_is_not_waited_on_forever(
    impatient: None,
) -> None:
    # Rather than hanging, which for a firmware install is the worst way to
    # fail: the user cannot tell it from a slow one.
    bootloader = FakeBootloader(hub_type=CITY_HUB)

    def silent(request: bytes) -> None:
        del request

    bootloader.handle = silent  # pyright: ignore[reportAttributeAccessIssue]

    with pytest.raises(TimeoutError):
        await powered_up.flash(
            FakeLink(bootloader), bytes(64), expect_hub_type=CITY_HUB
        )
