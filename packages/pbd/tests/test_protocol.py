# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The Pybricks hub protocol.

Values here are checked against the firmware header rather than against the
implementation, since agreeing with ourselves would prove nothing.
"""

from __future__ import annotations

import struct

import pytest

from pbd.hub import protocol as p

# Profile versions used throughout, named so the intent of each case is legible.
V12 = (1, 2, 0)
V13 = (1, 3, 0)
V14 = (1, 4, 0)
V15 = (1, 5, 0)


# ------------------------------------------------------------------- constants


def test_service_uuids_match_the_pybricks_profile() -> None:
    assert p.PYBRICKS_SERVICE_UUID == "c5f50001-8280-46da-89f4-6d8051e4aeef"
    assert p.PYBRICKS_COMMAND_EVENT_UUID == "c5f50002-8280-46da-89f4-6d8051e4aeef"
    assert p.PYBRICKS_HUB_CAPABILITIES_UUID == "c5f50003-8280-46da-89f4-6d8051e4aeef"


def test_standard_characteristic_uuids() -> None:
    # 0x2A26 is firmware revision and 0x2A28 is software revision; swapping
    # them reads the wrong string and misidentifies the protocol version.
    assert p.FIRMWARE_REVISION_UUID == "00002a26-0000-1000-8000-00805f9b34fb"
    assert p.SOFTWARE_REVISION_UUID == "00002a28-0000-1000-8000-00805f9b34fb"
    assert p.PNP_ID_UUID == "00002a50-0000-1000-8000-00805f9b34fb"


def test_command_values_match_the_firmware_header() -> None:
    assert (p.Command.STOP_USER_PROGRAM, p.Command.START_USER_PROGRAM) == (0, 1)
    assert (p.Command.START_REPL, p.Command.WRITE_USER_PROGRAM_META) == (2, 3)
    assert (p.Command.WRITE_USER_RAM, p.Command.REBOOT_TO_UPDATE_MODE) == (4, 5)
    assert (p.Command.WRITE_STDIN, p.Command.WRITE_APP_DATA) == (6, 7)


def test_event_values_match_the_firmware_header() -> None:
    assert (p.Event.STATUS_REPORT, p.Event.WRITE_STDOUT, p.Event.WRITE_APP_DATA) == (
        0,
        1,
        2,
    )


def test_status_flags_cover_every_documented_bit() -> None:
    # Bits 10 and 11 arrived in profile v1.5.0 and are missing from the tool
    # this replaces, so a hot battery went unreported there.
    assert p.StatusFlag.BATTERY_HIGH_TEMP_SHUTDOWN == 1 << 10
    assert p.StatusFlag.BATTERY_HIGH_TEMP_WARNING == 1 << 11
    assert p.StatusFlag.BLE_HOST_CONNECTED == 1 << 9
    assert p.StatusFlag.SHUTDOWN_REQUEST == 1 << 8


def test_user_program_ids() -> None:
    assert p.UserProgramId.LAST_SLOT == 127
    assert p.UserProgramId.REPL == 128
    assert p.UserProgramId.IMU_CALIBRATION == 130


def test_hub_error_values() -> None:
    assert p.HubError.INVALID_COMMAND == 0x80
    assert p.HubError.BUSY == 0x81


# -------------------------------------------------------------------- versions


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.4.0", (1, 4, 0)),
        ("1.5", (1, 5, 0)),
        ("2", (2, 0, 0)),
        (" 1.2.3 ", (1, 2, 3)),
        ("3.6.0-beta.1", (3, 6, 0)),
        ("1.4.0+build7", (1, 4, 0)),
    ],
)
def test_parse_version(text: str, expected: p.Version) -> None:
    assert p.parse_version(text) == expected


def test_parse_version_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="not a version"):
        p.parse_version("unknown")


# -------------------------------------------------------------------- encoding


def test_stop_is_a_single_byte() -> None:
    assert p.encode_stop_user_program() == bytes([p.Command.STOP_USER_PROGRAM])


def test_start_program_carries_a_slot_on_new_firmware() -> None:
    assert p.encode_start_user_program(3, profile=V14) == bytes(
        [p.Command.START_USER_PROGRAM, 3]
    )
    assert p.encode_start_user_program(3, profile=V15) == bytes(
        [p.Command.START_USER_PROGRAM, 3]
    )


def test_start_program_omits_the_slot_when_none_is_requested() -> None:
    # "Whatever is selected" is expressed by omitting the argument, on every
    # profile.
    assert p.encode_start_user_program(None, profile=V15) == bytes(
        [p.Command.START_USER_PROGRAM]
    )
    assert p.encode_start_user_program(None, profile=V12) == bytes(
        [p.Command.START_USER_PROGRAM]
    )


def test_start_program_refuses_a_slot_on_old_firmware() -> None:
    # Silently starting a different program than the one asked for would be
    # worse than failing.
    with pytest.raises(ValueError, match=r"1\.4\.0 or later"):
        p.encode_start_user_program(3, profile=V13)


def test_repl_uses_the_dedicated_command_only_on_old_firmware() -> None:
    # START_REPL was removed in v1.4.0 in favour of starting the REPL as a
    # program, so each side of that boundary needs a different command.
    assert p.encode_start_repl(profile=V13) == bytes([p.Command.START_REPL])
    assert p.encode_start_repl(profile=V14) == bytes(
        [p.Command.START_USER_PROGRAM, p.UserProgramId.REPL]
    )


def test_program_meta_uses_a_32_bit_size() -> None:
    assert p.encode_write_user_program_meta(0x11223344) == bytes(
        [p.Command.WRITE_USER_PROGRAM_META, 0x44, 0x33, 0x22, 0x11]
    )
    # Zero invalidates whatever is stored, so an interrupted download cannot
    # leave a runnable half-program.
    assert p.encode_write_user_program_meta(0) == bytes(
        [p.Command.WRITE_USER_PROGRAM_META, 0, 0, 0, 0]
    )


def test_user_ram_write_has_a_five_byte_header() -> None:
    payload = b"hello"
    encoded = p.encode_write_user_ram(0x0102, payload)
    assert encoded[0] == p.Command.WRITE_USER_RAM
    assert struct.unpack_from("<I", encoded, 1)[0] == 0x0102
    assert encoded[5:] == payload
    assert len(encoded) == len(payload) + p.WRITE_USER_RAM_HEADER_SIZE


def test_app_data_offset_is_only_sixteen_bits() -> None:
    # Narrower than the user RAM offset. Assuming they match produces a
    # corrupt buffer rather than an error.
    encoded = p.encode_write_app_data(0x0102, b"xy", profile=V14)
    assert encoded[0] == p.Command.WRITE_APP_DATA
    assert struct.unpack_from("<H", encoded, 1)[0] == 0x0102
    assert encoded[3:] == b"xy"
    assert len(encoded) == 2 + p.WRITE_APP_DATA_HEADER_SIZE


def test_app_data_rejects_an_offset_that_does_not_fit() -> None:
    with pytest.raises(ValueError, match="16 bits"):
        p.encode_write_app_data(0x10000, b"x", profile=V15)


def test_app_data_and_stdin_are_refused_on_older_firmware() -> None:
    with pytest.raises(ValueError, match="app data"):
        p.encode_write_app_data(0, b"x", profile=V13)
    with pytest.raises(ValueError, match="stdin"):
        p.encode_write_stdin(b"x", profile=V12)


def test_stdin_write_is_a_one_byte_header() -> None:
    assert p.encode_write_stdin(b"hi", profile=V13) == bytes(
        [p.Command.WRITE_STDIN, *b"hi"]
    )


def test_empty_payloads_are_allowed() -> None:
    assert p.encode_write_stdin(b"", profile=V14) == bytes([p.Command.WRITE_STDIN])
    assert len(p.encode_write_user_ram(0, b"")) == p.WRITE_USER_RAM_HEADER_SIZE


# -------------------------------------------------------------------- decoding


def _status(flags: int, *extra: int) -> bytes:
    return bytes([p.Event.STATUS_REPORT]) + struct.pack("<I", flags) + bytes(extra)


def test_status_report_decodes_flags() -> None:
    report = p.parse_status_report(_status(int(p.StatusFlag.USER_PROGRAM_RUNNING)))
    assert report.program_running
    assert report.flags == p.StatusFlag.USER_PROGRAM_RUNNING


def test_status_report_fields_absent_on_old_firmware_are_none() -> None:
    # Reporting None rather than guessing zero matters: slot zero is a real
    # slot, so a guess would be indistinguishable from a fact.
    report = p.parse_status_report(_status(0))
    assert report.running_program_id is None
    assert report.selected_slot is None


def test_status_report_reads_program_id_and_slot_when_present() -> None:
    with_program = p.parse_status_report(_status(0, 5))
    assert with_program.running_program_id == 5
    assert with_program.selected_slot is None

    full = p.parse_status_report(_status(0, 5, 2))
    assert (full.running_program_id, full.selected_slot) == (5, 2)


def test_status_report_rejects_a_truncated_payload() -> None:
    with pytest.raises(ValueError, match="too short"):
        p.parse_status_report(bytes([p.Event.STATUS_REPORT, 0, 0]))


def test_status_report_survives_unknown_future_flags() -> None:
    # A newer hub may set bits this build has never heard of; dropping the
    # connection over that would be worse than carrying them along.
    report = p.parse_status_report(_status(1 << 20))
    assert report.flags


# ---------------------------------------------------------------- capabilities


def test_capabilities_v15_includes_slots() -> None:
    data = struct.pack(
        "<HIIB", 512, int(p.FeatureFlag.USER_PROG_FORMAT_MULTI_MPY_V6), 8192, 5
    )
    caps = p.unpack_capabilities(data)
    assert (caps.max_write_size, caps.max_program_size, caps.num_slots) == (
        512,
        8192,
        5,
    )


def test_capabilities_v12_has_no_slots() -> None:
    data = struct.pack("<HII", 20, 0, 1024)
    caps = p.unpack_capabilities(data)
    assert caps.num_slots == 0
    assert caps.max_write_size == 20


def test_capabilities_rejects_a_short_value() -> None:
    with pytest.raises(ValueError, match="too short"):
        p.unpack_capabilities(b"\x00\x01\x02")


def test_max_payload_leaves_room_for_the_header() -> None:
    caps = p.unpack_capabilities(struct.pack("<HII", 20, 0, 1024))
    assert caps.max_user_ram_payload == 20 - p.WRITE_USER_RAM_HEADER_SIZE


@pytest.mark.parametrize(
    "flag",
    [
        p.FeatureFlag.USER_PROG_FORMAT_MULTI_MPY_V6,
        p.FeatureFlag.USER_PROG_FORMAT_MULTI_MPY_V6_1_NATIVE,
        p.FeatureFlag.USER_PROG_FORMAT_MULTI_MPY_V6_3_NATIVE,
    ],
)
def test_any_multi_file_flag_counts(flag: p.FeatureFlag) -> None:
    # Checking only the two older flags wrongly rejects a hub advertising just
    # the v6.3 native format. That is a real bug in the tool this replaces.
    caps = p.unpack_capabilities(struct.pack("<HII", 512, int(flag), 8192))
    assert caps.supports_multi_file


def test_no_multi_file_flag_means_unsupported() -> None:
    caps = p.unpack_capabilities(
        struct.pack("<HII", 512, int(p.FeatureFlag.BUILTIN_USER_PROGRAM_REPL), 8192)
    )
    assert not caps.supports_multi_file


def test_pnp_id_round_trip() -> None:
    pnp = p.unpack_pnp_id(struct.pack("<BHHH", 1, p.LEGO_COMPANY_ID, 0x0081, 0x0000))
    assert pnp.vendor_id == p.LEGO_COMPANY_ID
    assert pnp.product_id == 0x0081


def test_pnp_id_rejects_a_wrong_length() -> None:
    with pytest.raises(ValueError, match="7 bytes"):
        p.unpack_pnp_id(b"\x01\x02")


def _pnp(product: int, version: int = 0, *, vendor: int | None = None) -> p.PnpId:
    return p.PnpId(
        vendor_id_source=p.VendorIdSource.BLUETOOTH_SIG,
        vendor_id=p.LEGO_COMPANY_ID if vendor is None else vendor,
        product_id=product,
        product_version=version,
    )


@pytest.mark.parametrize(
    ("product", "version", "expected"),
    [
        (p.HubType.MOVE_HUB, 0, "Move hub"),
        (p.HubType.CITY_HUB, 0, "City hub"),
        (p.HubType.TECHNIC_HUB, 0, "Technic hub"),
        (p.HubType.TECHNIC_MOVE_HUB, 0, "Technic Move hub"),
        # One product id, two hubs on the shelf: the version says which.
        (p.HubType.TECHNIC_LARGE_HUB, 0, "Prime hub"),
        (p.HubType.TECHNIC_LARGE_HUB, 1, "Inventor hub"),
        (p.HubType.TECHNIC_SMALL_HUB, 0, "Essential hub"),
    ],
)
def test_hub_type_names_follow_pybricks_code(
    product: int, version: int, expected: str
) -> None:
    assert p.hub_type_name(_pnp(product, version)) == expected


def test_an_unknown_variant_of_a_known_hub_is_not_guessed_at() -> None:
    # Naming a future SPIKE variant after an existing one would be worse than
    # saying nothing, since the name is what a user checks they have.
    assert p.hub_type_name(_pnp(p.HubType.TECHNIC_LARGE_HUB, 7)) is None


def test_an_unknown_hub_has_no_name() -> None:
    assert p.hub_type_name(_pnp(0x99)) is None


def test_a_hub_from_another_vendor_has_no_name() -> None:
    assert p.hub_type_name(_pnp(p.HubType.CITY_HUB, vendor=0x1234)) is None


def test_a_hub_that_gave_no_pnp_id_has_no_name() -> None:
    assert p.hub_type_name(None) is None
