# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Turning a ``firmware.zip`` into the bytes a hub is given.

Pure work: no hub, no network, no files. That is deliberate, because it is
where the mistakes are expensive and where they are cheapest to test.

Everything is done here, before anything is connected to. A name that does not
fit, an image too large for the region it is checked over, metadata this build
cannot read -- all of them fail while the hub is still switched off, rather
than once it is sitting in a bootloader waiting to be written to.

Three metadata formats exist and all three are read. Version 3 is the one
current releases use, and it is the reason this returns a set of images rather
than one: a single ``primehub`` archive carries a build for the STM32F4 and a
build for the STM32H5, and which of them a hub wants is not known until the hub
is enumerated. Versions 1 and 2 named no platform because they had only one, so
they are lifted into the same shape.
"""

from __future__ import annotations

import json
import logging
import struct
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Final, cast

from pb_service.firmware.checksum import crc32, sum_complement
from pb_service.firmware.errors import (
    FirmwareBadArchiveError,
    FirmwareHubNameTooLongError,
)

logger = logging.getLogger(__name__)

__all__ = ["Firmware", "read_firmware"]

METADATA_NAME: Final = "firmware.metadata.json"
"""The one member every firmware archive has to have."""

LICENSE_NAME: Final = "ReadMe_OSS.txt"
"""The notices that come with the firmware, where there are any.

Not always present and not always non-empty -- the EV3 release ships it as a
zero-byte file -- so anything showing it has to cope with there being nothing
to show.
"""

DEFAULT_IMAGE_NAME: Final = "firmware-base.bin"
"""What versions 1 and 2 always called the image. Version 3 says which."""

_LEGACY_PLATFORMS: Final[Mapping[int, str]] = {
    0x40: "move_hub",
    0x41: "city_hub",
    0x80: "technic_hub",
    0x81: "prime_hub",
    0x83: "essential_hub",
    0xE1: "nxt",
    0xE2: "ev3",
}
"""The platform a pre-v3 archive meant but did not say.

Version 3 introduced platform names because one hub type had stopped meaning
one image. Before that the hub type was enough, so these are what those
archives would have said if they had been asked.
"""


@dataclass(frozen=True, slots=True)
class Firmware:
    """A firmware archive, read and ready to write."""

    hub_type: int
    """The ``device-id``, which is what decides how it is installed."""

    version: str
    """The firmware's own version, as the hub will report it."""

    metadata_version: str
    """Which format the archive was written in. Kept for diagnosis."""

    variants: Mapping[str, bytes]
    """Each platform's finished image, keyed by pbio platform name.

    More than one entry means the hub type covers more than one piece of
    hardware, and the right one has to be chosen from the device itself.
    """

    license_text: str = ""
    """The notices the archive carries, or empty when it carries none."""

    name_field: tuple[int, int] | None = None
    """Where a hub name goes and how much room it has, if anywhere.

    ``None`` for firmware that reserves no space for one, which is every
    official LEGO image.
    """

    @property
    def names_hubs(self) -> bool:
        """Whether this firmware can be given a hub name."""
        return self.name_field is not None


def _object_at(value: Any, what: str) -> Mapping[str, Any]:
    """Insist that a piece of decoded JSON is an object.

    Decoding gives back whatever the file happened to contain, and checking
    the shape here rather than at each field means the rest of this module can
    read it as a mapping without every access being a question.

    Args:
        value: The decoded value.
        what: What it was supposed to be, for the message.

    Returns:
        The same value, as a mapping.

    Raises:
        FirmwareBadArchiveError: If it is anything else.
    """
    if not isinstance(value, dict):
        raise FirmwareBadArchiveError(f"{what} is not an object")
    return cast("dict[str, Any]", value)


def _int_at(source: Mapping[str, Any], key: str, *, default: int = 0) -> int:
    """Read an integer out of decoded JSON without trusting it to be one.

    Args:
        source: The decoded object.
        key: Which field.
        default: What an absent field means.

    Returns:
        The value.

    Raises:
        FirmwareBadArchiveError: If the field is present but not a number.
    """
    value = source.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise FirmwareBadArchiveError(f"{key} is not a number in {METADATA_NAME}")
    return value


def _str_at(source: Mapping[str, Any], key: str, *, default: str = "") -> str:
    """Read a string out of decoded JSON without trusting it to be one.

    Args:
        source: The decoded object.
        key: Which field.
        default: What an absent field means.

    Returns:
        The value.

    Raises:
        FirmwareBadArchiveError: If the field is present but not text.
    """
    value = source.get(key, default)
    if not isinstance(value, str):
        raise FirmwareBadArchiveError(f"{key} is not text in {METADATA_NAME}")
    return value


def _named(image: bytearray, name: str, offset: int, size: int) -> None:
    """Write a hub name into an image, in place.

    Args:
        image: The image so far.
        name: The name to give the hub.
        offset: Where the field starts.
        size: How many bytes the field holds, terminator included.

    Raises:
        FirmwareHubNameTooLongError: If the encoded name does not fit.
    """
    encoded = name.encode() + b"\0"
    if len(encoded) > size:
        raise FirmwareHubNameTooLongError(
            f"{name!r} needs {len(encoded)} bytes with its terminator; "
            f"this firmware has room for {size}"
        )
    image[offset : offset + len(encoded)] = encoded


def _checked(image: bytearray, kind: str, region: int) -> bytes:
    """Append the checksum an image says it wants.

    Args:
        image: The image, complete but for its checksum.
        kind: What the metadata called for.
        region: The region the hub will check, in bytes.

    Returns:
        The finished image.

    Raises:
        FirmwareBadArchiveError: If the checksum is one we cannot compute, or
            the image does not fit the region it is checked over.
    """
    if kind == "none":
        return bytes(image)
    if kind not in ("sum", "crc32"):
        raise FirmwareBadArchiveError(f"unsupported checksum type: {kind}")
    try:
        value = (
            sum_complement(bytes(image), region)
            if kind == "sum"
            else crc32(bytes(image), region)
        )
    except ValueError as exc:
        raise FirmwareBadArchiveError(str(exc)) from exc
    return bytes(image) + struct.pack("<I", value)


def _member(archive: zipfile.ZipFile, name: str) -> bytes:
    """Read one member, naming the missing one rather than raising ``KeyError``.

    Args:
        archive: The open archive.
        name: Which member.

    Returns:
        Its contents.

    Raises:
        FirmwareBadArchiveError: If the archive does not have it.
    """
    try:
        return archive.read(name)
    except KeyError as exc:
        raise FirmwareBadArchiveError(f"{name} is missing from the archive") from exc


def _build(
    archive: zipfile.ZipFile,
    spec: Mapping[str, Any],
    *,
    member: str,
    name: str | None,
) -> tuple[bytes, tuple[int, int] | None]:
    """Assemble one image from the metadata describing it.

    Args:
        archive: The open archive.
        spec: The metadata for this image -- the whole document for versions 1
            and 2, one entry of ``variants`` for version 3.
        member: Which member holds the base image.
        name: A hub name to write into it, where it takes one.

    Returns:
        The finished image, and where a name goes if anywhere.

    Raises:
        FirmwareBadArchiveError: If the metadata cannot be followed.
        FirmwareHubNameTooLongError: If the name does not fit.
    """
    image = bytearray(_member(archive, member))

    # Version 1 kept a length-prefixed program after the firmware. Appending
    # one has not been supported for years -- both current implementations
    # write a length of zero -- but the field still has to be there, because
    # the firmware reads it either way.
    if spec.get("user-mpy-offset") is not None:
        offset = _int_at(spec, "user-mpy-offset")
        image.extend(bytes(max(0, offset - len(image))))
        image.extend(struct.pack("<I", 0))
        image.extend(bytes(-len(image) % 4))

    # Version 1.1 called the size field something else, and 1.0 had neither.
    where = _int_at(spec, "hub-name-offset")
    room = _int_at(spec, "hub-name-size") or _int_at(spec, "max-hub-name-size")
    field = (where, room) if where and room else None

    if name and field is not None:
        _named(image, name, *field)

    region = _int_at(spec, "checksum-size") or _int_at(spec, "max-firmware-size")
    kind = _str_at(spec, "checksum-type", default="none")
    return _checked(image, kind, region), field


def _variants_v3(
    archive: zipfile.ZipFile, metadata: Mapping[str, Any], name: str | None
) -> tuple[dict[str, bytes], tuple[int, int] | None]:
    """Build every image a version 3 archive declares.

    Args:
        archive: The open archive.
        metadata: The whole metadata document.
        name: A hub name to write into each image.

    Returns:
        The images by platform, and where a name goes if anywhere.

    Raises:
        FirmwareBadArchiveError: If the variants are not as declared.
        FirmwareHubNameTooLongError: If the name does not fit every variant.
    """
    declared = metadata.get("variants")
    if not isinstance(declared, list) or not declared:
        raise FirmwareBadArchiveError("a version 3 archive with no variants")

    built: dict[str, bytes] = {}
    field: tuple[int, int] | None = None
    entries = cast("list[Any]", declared)
    for entry in entries:
        spec = _object_at(entry, "a variant")
        platform = _str_at(spec, "platform")
        if not platform:
            raise FirmwareBadArchiveError("a variant with no platform")
        # Named by the metadata and never guessed from the file name: the
        # LEGO restore archive calls its H5 image "spike-f5-...", for the
        # platform called prime_hub_h5.
        member = _str_at(spec, "firmware", default=DEFAULT_IMAGE_NAME)
        built[platform], theirs = _build(archive, spec, member=member, name=name)
        # Every variant is checked against its own field as it is built, so
        # this is only what gets reported afterwards. Keep the smallest, so
        # that "there is room for N" cannot promise more than some variant
        # would actually accept.
        if theirs is not None and (field is None or theirs[1] < field[1]):
            field = theirs
    return built, field


def read_firmware(archive_bytes: bytes, *, name: str | None = None) -> Firmware:
    """Read a firmware archive and build every image in it.

    Every variant is built, not just the one that will be used, because which
    one is wanted is not known until the hub has been put into update mode and
    enumerated. So a name that will not fit, or an image too large for its
    region, is found while the hub is still switched off and the user has not
    yet been sent off to hold a button down.

    Args:
        archive_bytes: The contents of a ``firmware.zip``.
        name: A name to give the hub, where the firmware supports one. Ignored
            by firmware that does not, which is every official LEGO image.

    Returns:
        What the archive holds.

    Raises:
        FirmwareBadArchiveError: If it cannot be read as firmware.
        FirmwareHubNameTooLongError: If the name does not fit every variant.
    """
    try:
        opened = zipfile.ZipFile(BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise FirmwareBadArchiveError(f"not a zip file: {exc}") from exc

    with opened as archive:
        try:
            document: Any = json.loads(_member(archive, METADATA_NAME))
        except json.JSONDecodeError as exc:
            raise FirmwareBadArchiveError(
                f"{METADATA_NAME} is not JSON: {exc}"
            ) from exc
        metadata = _object_at(document, METADATA_NAME)

        if "main.py" in archive.namelist():
            # Version 1 could append one. Saying so is better than quietly
            # producing firmware without the program somebody put in the zip.
            logger.warning("ignoring main.py: firmware cannot carry a program")

        version = _str_at(metadata, "metadata-version")
        hub_type = _int_at(metadata, "device-id")

        if version.startswith("3."):
            built, field = _variants_v3(archive, metadata, name)
        elif version.startswith(("1.", "2.")):
            platform = _LEGACY_PLATFORMS.get(hub_type)
            if platform is None:
                raise FirmwareBadArchiveError(f"unknown hub type: {hub_type:#04x}")
            image, field = _build(
                archive, metadata, member=DEFAULT_IMAGE_NAME, name=name
            )
            built = {platform: image}
        else:
            raise FirmwareBadArchiveError(f"unsupported metadata version: {version}")

        license_text = ""
        if LICENSE_NAME in archive.namelist():
            license_text = archive.read(LICENSE_NAME).decode("utf-8", "replace")

    return Firmware(
        hub_type=hub_type,
        version=_str_at(metadata, "firmware-version"),
        metadata_version=version,
        variants=built,
        license_text=license_text,
        name_field=field,
    )
