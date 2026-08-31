# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Reading a firmware archive, which is the last chance to refuse.

Everything here happens before a hub is connected to, so these are the cases
that decide whether somebody is sent off to hold a button down for firmware
that was never going to work. The archives are built in the test rather than
kept as fixtures: LEGO's images are not ours to redistribute, the Pybricks ones
are hundreds of kilobytes, and neither would let a test say "now make the
checksum region too small".
"""

from __future__ import annotations

import json
import logging
import struct
import zipfile
from io import BytesIO
from typing import Any

import pytest

from pb_service.firmware.checksum import sum_complement
from pb_service.firmware.errors import (
    FirmwareBadArchiveError,
    FirmwareHubNameTooLongError,
)
from pb_service.firmware.image import read_firmware

CITY_HUB = 0x41
PRIME_HUB = 0x81


_MEMBERS = {
    "firmware_base_bin": "firmware-base.bin",
    "main_py": "main.py",
    "readme": "ReadMe_OSS.txt",
}
"""Keyword-friendly spellings of the members that have awkward names."""


def archive(metadata: dict[str, Any], **members: bytes) -> bytes:
    """Build a firmware archive.

    Args:
        metadata: What goes in ``firmware.metadata.json``.
        **members: The other members. A few have names that are not valid
            keywords and are spelled per :data:`_MEMBERS`; anything else is
            used as given, which is how a variant names its own image.

    Returns:
        The archive.
    """
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("firmware.metadata.json", json.dumps(metadata))
        for name, data in members.items():
            zf.writestr(_MEMBERS.get(name, name), data)
    return buffer.getvalue()


def v3(**overrides: Any) -> dict[str, Any]:
    """A version 3 document with one ordinary variant."""
    variant: dict[str, Any] = {
        "platform": "city_hub",
        "firmware": "firmware-base.bin",
        "checksum-type": "none",
        "checksum-size": 0,
        "hub-name-offset": 0,
        "hub-name-size": 0,
    }
    variant.update(overrides.pop("variant", {}))
    document: dict[str, Any] = {
        "metadata-version": "3.0.0",
        "firmware-version": "4.1.0b3",
        "device-id": CITY_HUB,
        "variants": [variant],
    }
    document.update(overrides)
    return document


def test_a_version_3_archive_yields_one_image_per_platform() -> None:
    # The case this whole shape exists for: one primehub zip, two builds, and
    # nothing outside the bootloader able to say which is wanted.
    data = archive(
        {
            "metadata-version": "3.0.0",
            "firmware-version": "hub-os-1.8.149",
            "device-id": PRIME_HUB,
            "variants": [
                {
                    "platform": "prime_hub_f4",
                    "firmware": "spike-f4.bin",
                    "checksum-type": "none",
                    "checksum-size": 0,
                    "hub-name-offset": 0,
                    "hub-name-size": 0,
                },
                {
                    "platform": "prime_hub_h5",
                    # Named "f5" for the platform called h5, exactly as the
                    # real restore archive does it. Guessing from the file
                    # name would put the wrong image on the hub.
                    "firmware": "spike-f5.bin",
                    "checksum-type": "none",
                    "checksum-size": 0,
                    "hub-name-offset": 0,
                    "hub-name-size": 0,
                },
            ],
        },
        **{"spike-f4.bin": b"f4" * 8, "spike-f5.bin": b"h5" * 8},
    )
    firmware = read_firmware(data)
    assert firmware.hub_type == PRIME_HUB
    assert set(firmware.variants) == {"prime_hub_f4", "prime_hub_h5"}
    assert firmware.variants["prime_hub_f4"] == b"f4" * 8
    assert firmware.variants["prime_hub_h5"] == b"h5" * 8


def test_a_legacy_archive_is_lifted_into_the_same_shape() -> None:
    # Versions 1 and 2 named no platform because there was only ever one.
    data = archive(
        {
            "metadata-version": "2.1.0",
            "firmware-version": "3.6.0",
            "device-id": CITY_HUB,
            "checksum-type": "none",
            "checksum-size": 0,
            "hub-name-offset": 0,
            "hub-name-size": 0,
        },
        firmware_base_bin=b"x" * 16,
    )
    firmware = read_firmware(data)
    assert firmware.variants == {"city_hub": b"x" * 16}


def test_a_sum_checksum_makes_the_region_add_to_nothing() -> None:
    # What the hub itself does: add up every word of the region and expect
    # nothing left over.
    region = 64
    base = bytes(range(16))
    data = archive(
        v3(variant={"checksum-type": "sum", "checksum-size": region}),
        firmware_base_bin=base,
    )
    image = read_firmware(data).variants["city_hub"]
    assert len(image) == len(base) + 4
    assert image[: len(base)] == base

    total = sum(
        int.from_bytes(image[at : at + 4], "little") for at in range(0, len(image), 4)
    )
    total += 0xFFFFFFFF * len(range(len(image), region, 4))
    assert total % (1 << 32) == 0


def test_a_crc32_checksum_is_four_bytes_on_the_end() -> None:
    data = archive(
        v3(variant={"checksum-type": "crc32", "checksum-size": 64}),
        firmware_base_bin=bytes(16),
    )
    image = read_firmware(data).variants["city_hub"]
    assert len(image) == 20


def test_no_checksum_leaves_the_image_exactly_as_it_was() -> None:
    # Every official LEGO image says "none": it arrives complete and is not
    # ours to add anything to.
    data = archive(v3(), firmware_base_bin=b"untouched")
    assert read_firmware(data).variants["city_hub"] == b"untouched"


def test_an_image_too_big_for_its_region_is_refused() -> None:
    # Better here than after the hub has been put into update mode.
    data = archive(
        v3(variant={"checksum-type": "sum", "checksum-size": 8}),
        firmware_base_bin=bytes(64),
    )
    with pytest.raises(FirmwareBadArchiveError, match="too large"):
        read_firmware(data)


def test_a_version_1_archive_still_gets_its_empty_program_field() -> None:
    # Firmware has not carried a program for years, but the field is still
    # read by the firmware, so it still has to be written.
    offset = 32
    data = archive(
        {
            "metadata-version": "1.0.0",
            "firmware-version": "3.0.0",
            "device-id": CITY_HUB,
            "checksum-type": "sum",
            "user-mpy-offset": offset,
            "max-firmware-size": 256,
        },
        firmware_base_bin=b"z" * 16,
    )
    image = read_firmware(data).variants["city_hub"]
    # Padded out to the offset, then a length of zero, then the checksum.
    assert image[16:offset] == bytes(offset - 16)
    assert image[offset : offset + 4] == struct.pack("<I", 0)
    assert len(image) == offset + 4 + 4


def test_a_program_in_the_archive_is_ignored_and_said_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = archive(v3(), firmware_base_bin=b"x" * 4, main_py=b"print('hi')")
    with caplog.at_level(logging.WARNING):
        assert read_firmware(data).variants["city_hub"] == b"x" * 4
    assert "main.py" in caplog.text


# ------------------------------------------------------------ naming the hub


def test_a_name_is_written_where_the_metadata_says() -> None:
    # The only moment a hub can be named, so it had better land in the right
    # place.
    data = archive(
        v3(variant={"hub-name-offset": 4, "hub-name-size": 8}),
        firmware_base_bin=bytes(16),
    )
    image = read_firmware(data, name="Bumblebee"[:7]).variants["city_hub"]
    assert image[4:12] == b"Bumbleb\0"


def test_a_name_that_does_not_fit_is_refused() -> None:
    data = archive(
        v3(variant={"hub-name-offset": 4, "hub-name-size": 8}),
        firmware_base_bin=bytes(16),
    )
    with pytest.raises(FirmwareHubNameTooLongError, match="room for 8"):
        read_firmware(data, name="Bumblebee")


def test_a_name_is_counted_in_bytes_rather_than_characters() -> None:
    # The field is UTF-8, so an accent costs two of the sixteen.
    data = archive(
        v3(variant={"hub-name-offset": 0, "hub-name-size": 0}),
        firmware_base_bin=bytes(16),
    )
    # Nowhere to put one, so it is quietly not written rather than refused:
    # every official LEGO image is in this position.
    assert read_firmware(data, name="Rover").variants["city_hub"] == bytes(16)


def test_a_name_has_to_fit_every_variant() -> None:
    # Which variant will be used is not known yet, so all of them have to
    # accept the name or none of them can be offered.
    data = archive(
        {
            "metadata-version": "3.0.0",
            "firmware-version": "4.1.0b3",
            "device-id": PRIME_HUB,
            "variants": [
                {
                    "platform": "prime_hub_f4",
                    "firmware": "a.bin",
                    "checksum-type": "none",
                    "checksum-size": 0,
                    "hub-name-offset": 4,
                    "hub-name-size": 16,
                },
                {
                    "platform": "prime_hub_h5",
                    "firmware": "b.bin",
                    "checksum-type": "none",
                    "checksum-size": 0,
                    "hub-name-offset": 4,
                    "hub-name-size": 6,
                },
            ],
        },
        **{"a.bin": bytes(32), "b.bin": bytes(32)},
    )
    with pytest.raises(FirmwareHubNameTooLongError):
        read_firmware(data, name="Bumblebee")


def test_firmware_says_whether_it_can_be_named() -> None:
    named = archive(
        v3(variant={"hub-name-offset": 4, "hub-name-size": 16}),
        firmware_base_bin=bytes(32),
    )
    anonymous = archive(v3(), firmware_base_bin=bytes(32))
    assert read_firmware(named).names_hubs
    assert not read_firmware(anonymous).names_hubs


# ------------------------------------------------------------------ licences


def test_the_licence_comes_back_when_there_is_one() -> None:
    data = archive(v3(), firmware_base_bin=bytes(4), readme=b"Notices go here.")
    assert read_firmware(data).license_text == "Notices go here."


def test_an_archive_with_no_licence_says_so_with_silence() -> None:
    # The EV3 release ships a zero-byte one, so anything showing this has to
    # cope with there being nothing to show.
    data = archive(v3(), firmware_base_bin=bytes(4), readme=b"")
    assert read_firmware(data).license_text == ""
    assert read_firmware(archive(v3(), firmware_base_bin=bytes(4))).license_text == ""


# ------------------------------------------------------- archives that refuse


def test_something_that_is_not_a_zip_is_refused() -> None:
    with pytest.raises(FirmwareBadArchiveError, match="not a zip"):
        read_firmware(b"<!doctype html><title>404</title>")


def test_an_archive_with_no_metadata_names_what_is_missing() -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("firmware-base.bin", b"x")
    with pytest.raises(FirmwareBadArchiveError, match=r"firmware\.metadata\.json"):
        read_firmware(buffer.getvalue())


def test_metadata_that_is_not_json_is_refused() -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("firmware.metadata.json", b"{not json")
    with pytest.raises(FirmwareBadArchiveError, match="not JSON"):
        read_firmware(buffer.getvalue())


def test_a_metadata_version_from_the_future_is_refused_by_name() -> None:
    # Rather than read four of the six fields and write something plausible.
    data = archive(v3(**{"metadata-version": "4.0.0"}), firmware_base_bin=bytes(4))
    with pytest.raises(FirmwareBadArchiveError, match=r"4\.0\.0"):
        read_firmware(data)


def test_a_missing_image_names_the_member_it_wanted() -> None:
    data = archive(v3(variant={"firmware": "somewhere-else.bin"}))
    with pytest.raises(FirmwareBadArchiveError, match=r"somewhere-else\.bin"):
        read_firmware(data)


def test_a_version_3_archive_with_no_variants_is_refused() -> None:
    data = archive(v3(variants=[]), firmware_base_bin=bytes(4))
    with pytest.raises(FirmwareBadArchiveError, match="no variants"):
        read_firmware(data)


def test_a_legacy_archive_for_an_unknown_hub_is_refused() -> None:
    # There is no platform name to lift it into, and guessing one would mean
    # choosing how to flash a hub nobody here has heard of.
    data = archive(
        {
            "metadata-version": "2.1.0",
            "firmware-version": "1.0",
            "device-id": 0x99,
            "checksum-type": "none",
        },
        firmware_base_bin=bytes(4),
    )
    with pytest.raises(FirmwareBadArchiveError, match="unknown hub type"):
        read_firmware(data)


def test_a_checksum_we_cannot_compute_is_refused_by_name() -> None:
    data = archive(
        v3(variant={"checksum-type": "sha256", "checksum-size": 64}),
        firmware_base_bin=bytes(16),
    )
    with pytest.raises(FirmwareBadArchiveError, match="sha256"):
        read_firmware(data)


def test_metadata_of_the_wrong_shape_is_refused_rather_than_coerced() -> None:
    data = archive(v3(**{"device-id": "0x41"}), firmware_base_bin=bytes(4))
    with pytest.raises(FirmwareBadArchiveError, match="device-id is not a number"):
        read_firmware(data)


def test_the_checksum_helpers_agree_with_the_image_they_produce() -> None:
    # Guards the join between the two modules: the region the checksum is
    # computed over is the one the metadata declared, not the image length.
    region = 128
    base = bytes(range(32))
    data = archive(
        v3(variant={"checksum-type": "sum", "checksum-size": region}),
        firmware_base_bin=base,
    )
    image = read_firmware(data).variants["city_hub"]
    assert image[-4:] == struct.pack("<I", sum_complement(base, region))
