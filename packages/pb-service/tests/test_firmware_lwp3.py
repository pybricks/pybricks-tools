# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The LEGO bootloader's wire format.

Every expectation here is a literal, taken from the bootloader protocol as
``pybricksdev`` and ``tools/flash`` implement it, rather than computed the same
way the code under test computes it. A test that builds its expectation with
the code's own helpers proves only that the helper is deterministic.
"""

from __future__ import annotations

import struct

import pytest

from pb_service.firmware.bootloader import lwp3

MOVE_HUB = 0x40
CITY_HUB = 0x41
TECHNIC_HUB = 0x80

BOOTLOADER = frozenset({lwp3.BOOTLOADER_SERVICE_UUID})
LEGO_FIRMWARE = frozenset({lwp3.HUB_SERVICE_UUID})


def test_the_uuids_are_legos() -> None:
    # Written out rather than built, because a wrong UUID means scanning
    # silently finds nothing and there is no other symptom.
    assert lwp3.HUB_SERVICE_UUID == "00001623-1212-efde-1623-785feabcd123"
    assert lwp3.HUB_CHARACTERISTIC_UUID == "00001624-1212-efde-1623-785feabcd123"
    assert lwp3.BOOTLOADER_SERVICE_UUID == "00001625-1212-efde-1623-785feabcd123"
    assert lwp3.BOOTLOADER_CHARACTERISTIC_UUID == "00001626-1212-efde-1623-785feabcd123"
    assert lwp3.LEGO_COMPANY_ID == 0x0397


# --------------------------------------------------- recognising a hub found


def test_a_hub_in_its_bootloader_is_recognised() -> None:
    # Four bytes of bootloader version, the hub type, then capabilities.
    data = struct.pack("<IBB", 0x01000000, CITY_HUB, 0x00)
    found = lwp3.read_advertisement(data, BOOTLOADER)
    assert found is not None
    assert found.in_bootloader
    assert found.hub_type == CITY_HUB
    assert found.version == 0x01000000


def test_a_hub_running_legos_firmware_is_recognised() -> None:
    # Button, hub type, capabilities, last network, status, and one spare.
    data = bytes([0x01, TECHNIC_HUB, 0x00, 0x00, 0x00, 0x00])
    found = lwp3.read_advertisement(data, LEGO_FIRMWARE)
    assert found is not None
    assert not found.in_bootloader
    assert found.hub_type == TECHNIC_HUB
    assert found.button_pressed


def test_a_pybricks_hub_is_not_a_lego_advertisement() -> None:
    # It advertises its own service and a Plug and Play id instead, so this
    # has nothing to say about it and says so rather than guessing.
    data = bytes(6)
    assert (
        lwp3.read_advertisement(
            data, frozenset({"c5f50001-8280-46da-89f4-6d8051e4aeef"})
        )
        is None
    )


def test_an_advertisement_of_the_wrong_length_is_not_read() -> None:
    # Somebody else's manufacturer data, or a truncated packet. Reading it
    # anyway would invent a hub type out of whatever byte happened to be
    # there.
    assert lwp3.read_advertisement(bytes(5), BOOTLOADER) is None
    assert lwp3.read_advertisement(bytes(7), BOOTLOADER) is None


# ---------------------------------------------------------------- the wire


def test_rebooting_a_lego_hub_sends_the_safety_key() -> None:
    # The key is what stops this happening by accident, so it is checked
    # byte for byte.
    assert lwp3.encode_reboot_to_bootloader() == b"\x0c\x00\x10LPF2-Boot"


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        (lwp3.encode_get_info(), b"\x55"),
        (lwp3.encode_erase(), b"\x11"),
        (lwp3.encode_get_checksum(), b"\x66"),
        (lwp3.encode_start_app(), b"\x33"),
    ],
)
def test_the_bare_commands_are_one_byte(encoded: bytes, expected: bytes) -> None:
    assert encoded == expected


def test_getting_ready_says_how_much_is_coming() -> None:
    assert lwp3.encode_init_loader(0x00012345) == b"\x44\x45\x23\x01\x00"


def test_a_write_carries_its_length_and_address() -> None:
    # The length counts the four address bytes as well as the data, which is
    # the detail worth pinning: an off-by-four here writes firmware to the
    # wrong place.
    payload = bytes(range(8))
    assert lwp3.encode_program(0x08005000, payload) == (
        b"\x22" + bytes([len(payload) + 4]) + b"\x00\x50\x00\x08" + payload
    )


def test_what_the_bootloader_says_about_itself_is_read() -> None:
    reply = bytes([lwp3.BootloaderCommand.GET_INFO]) + struct.pack(
        "<iIIB", 7, 0x08005000, 0x0801FFFF, CITY_HUB
    )
    info = lwp3.read_info(reply)
    assert info.version == 7
    assert info.start_address == 0x08005000
    assert info.end_address == 0x0801FFFF
    assert info.hub_type == CITY_HUB


def test_a_reply_to_a_different_question_is_refused() -> None:
    # Rather than reading an erase result as a memory layout.
    reply = bytes([lwp3.BootloaderCommand.ERASE_FLASH]) + struct.pack(
        "<iIIB", 7, 0, 0, CITY_HUB
    )
    with pytest.raises(ValueError, match="not a bootloader info reply"):
        lwp3.read_info(reply)


def test_a_truncated_reply_is_refused() -> None:
    with pytest.raises(ValueError, match="not a bootloader info reply"):
        lwp3.read_info(bytes([lwp3.BootloaderCommand.GET_INFO, 0x00]))


def test_the_last_write_is_answered_with_a_checksum_and_a_count() -> None:
    reply = bytes([lwp3.BootloaderCommand.PROGRAM_FLASH]) + struct.pack(
        "<BI", 0xAB, 123456
    )
    assert lwp3.read_program_reply(reply) == (0xAB, 123456)


def test_something_that_is_not_a_flash_reply_is_refused() -> None:
    with pytest.raises(ValueError, match="not a flash reply"):
        lwp3.read_program_reply(bytes([lwp3.BootloaderCommand.GET_CHECKSUM, 0]))


# ------------------------------------------------------------ write sizes


@pytest.mark.parametrize(
    ("hub_type", "expected"),
    [(MOVE_HUB, 14), (CITY_HUB, 32), (TECHNIC_HUB, 32)],
)
def test_each_hub_takes_its_own_size_of_write(hub_type: int, expected: int) -> None:
    # Fourteen on the Move hub because its Bluetooth maximum transmission
    # unit is 23 and cannot be raised. Too large a write does not fail
    # cleanly; it fails part way through a flash.
    info = lwp3.BootloaderInfo(
        version=7, start_address=0, end_address=0, hub_type=hub_type
    )
    assert info.payload_size == expected


def test_a_hub_nobody_has_heard_of_gets_the_smallest_write() -> None:
    # Slowly beats refusing, and beats guessing large and failing halfway.
    info = lwp3.BootloaderInfo(version=7, start_address=0, end_address=0, hub_type=0xFF)
    assert info.payload_size == min(lwp3.PAYLOAD_SIZES.values())
