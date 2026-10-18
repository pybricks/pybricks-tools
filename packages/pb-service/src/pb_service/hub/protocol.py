# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The Pybricks hub protocol.

Written against ``lib/pbio/include/pbio/protocol.h`` in the firmware, which is
authoritative, cross-checked against ``pybricks-code``'s
``src/ble-pybricks-service/protocol.ts``.

Every command and event records the profile version that introduced it, and the
encoders refuse or fall back explicitly rather than sending bytes a given
firmware will reject. That gating is not defensive padding: the shape of
``START_USER_PROGRAM`` changed in v1.4.0, ``START_REPL`` was removed in the same
release, and the status report grew fields in v1.4.0 and again in v1.5.0. Getting
it wrong produces confusing failures against older hubs.

Deliberately transport-agnostic: the USB interface carries the same commands and
events, so nothing here knows about Bluetooth.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import Final

__all__ = [
    "PYBRICKS_COMMAND_EVENT_UUID",
    "PYBRICKS_HUB_CAPABILITIES_UUID",
    "PYBRICKS_SERVICE_UUID",
    "Capabilities",
    "Command",
    "Event",
    "FeatureFlag",
    "HubError",
    "HubType",
    "StatusFlag",
    "StatusReport",
    "UserProgramId",
    "Version",
    "encode_reboot_to_update_mode",
    "encode_start_repl",
    "encode_start_user_program",
    "encode_stop_user_program",
    "encode_write_app_data",
    "encode_write_stdin",
    "encode_write_user_program_meta",
    "encode_write_user_ram",
    "hub_type_name",
    "parse_status_report",
    "parse_version",
    "start_takes_program_id",
    "unpack_capabilities",
    "unpack_pnp_id",
]

Version = tuple[int, int, int]

# --------------------------------------------------------------------- UUIDs


def _pybricks_uuid(short: int) -> str:
    return f"c5f5{short:04x}-8280-46da-89f4-6d8051e4aeef"


def _standard_uuid(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


PYBRICKS_SERVICE_UUID: Final = _pybricks_uuid(0x0001)
PYBRICKS_COMMAND_EVENT_UUID: Final = _pybricks_uuid(0x0002)
PYBRICKS_HUB_CAPABILITIES_UUID: Final = _pybricks_uuid(0x0003)

DEVICE_INFORMATION_SERVICE_UUID: Final = _standard_uuid(0x180A)
DEVICE_NAME_UUID: Final = _standard_uuid(0x2A00)
FIRMWARE_REVISION_UUID: Final = _standard_uuid(0x2A26)
SOFTWARE_REVISION_UUID: Final = _standard_uuid(0x2A28)
"""Carries the Pybricks profile version, not the firmware version."""

PNP_ID_UUID: Final = _standard_uuid(0x2A50)

LEGO_COMPANY_ID: Final = 0x0397

# ------------------------------------------------------------------ versions

MIN_SUPPORTED_PROFILE: Final[Version] = (1, 2, 0)
"""Oldest profile this service speaks.

v1.2.0 introduced the capabilities characteristic and RAM-based program
download, so requiring it removes the entire legacy Nordic UART code path.
"""

MAX_KNOWN_PROFILE: Final[Version] = (1, 7, 0)
"""Newest profile this file has been written against.

The firmware header declares v1.6.0, whose addition was a USB interface: the
same commands and events inside a message envelope a byte stream needs, which
belongs to :mod:`pb_service.hub.stream.wire` rather than here. v1.7.0 is the
telemetry pair below, where the two upstreams disagree -- the firmware carries
it as unreleased, Pybricks Code already calls it v1.7.0 -- and the higher
number is the safer of the two to claim, since claiming it only ever admits
knowing about values that do exist.
"""

_PROFILE_SLOT_ARGUMENT: Final[Version] = (1, 4, 0)
"""START_USER_PROGRAM began taking a program id in v1.4.0."""

_PROFILE_STATUS_PROGRAM_ID: Final[Version] = (1, 4, 0)
_PROFILE_STATUS_SLOT: Final[Version] = (1, 5, 0)


def parse_version(text: str) -> Version:
    """Parse a dotted version string.

    Args:
        text: For example ``"1.4.0"``. Trailing build metadata is ignored.

    Returns:
        Major, minor and patch numbers, with missing components read as zero.

    Raises:
        ValueError: If no leading numeric component can be read.
    """
    cleaned = text.strip().split("+", 1)[0].split("-", 1)[0]
    parts = cleaned.split(".")
    numbers: list[int] = []

    for part in parts[:3]:
        digits = "".join(c for c in part if c.isdigit())
        if not digits:
            break

        numbers.append(int(digits))

    if not numbers:
        raise ValueError(f"not a version: {text!r}")

    while len(numbers) < 3:
        numbers.append(0)

    return (numbers[0], numbers[1], numbers[2])


# -------------------------------------------------------------------- values


class Command(IntEnum):
    """Commands written to the command/event characteristic."""

    STOP_USER_PROGRAM = 0
    START_USER_PROGRAM = 1
    START_REPL = 2
    """Removed in profile v1.4.0; use START_USER_PROGRAM with REPL instead."""

    WRITE_USER_PROGRAM_META = 3
    WRITE_USER_RAM = 4
    REBOOT_TO_UPDATE_MODE = 5
    WRITE_STDIN = 6
    WRITE_APP_DATA = 7
    WRITE_TELEMETRY = 8
    """Since profile v1.7.0. Telemetry requests, such as changing a sensor mode.

    The payload is one or more telemetry commands, whose format the firmware
    keeps in its own ``telemetry.h`` and calls provisional. Nothing here sends
    one; the value is named so that the command space is complete.
    """


class Event(IntEnum):
    """Notifications received from the command/event characteristic."""

    STATUS_REPORT = 0
    WRITE_STDOUT = 1
    WRITE_APP_DATA = 2
    """Data the program sends to an app, kept out of the user terminal."""

    WRITE_TELEMETRY = 3
    """Telemetry the hub streams to the host, since profile v1.7.0.

    Named so that a connection can tell "an event we have chosen not to handle"
    from "an event from a firmware newer than this build", which are the same
    byte to anything that only knows the events it consumes.
    """


class StatusFlag(IntFlag):
    """Hub status bits, as carried in a status report."""

    BATTERY_LOW_VOLTAGE_WARNING = 1 << 0
    BATTERY_LOW_VOLTAGE_SHUTDOWN = 1 << 1
    BATTERY_HIGH_CURRENT = 1 << 2
    BLE_ADVERTISING = 1 << 3
    BLE_LOW_SIGNAL = 1 << 4
    POWER_BUTTON_PRESSED = 1 << 5
    USER_PROGRAM_RUNNING = 1 << 6
    SHUTDOWN = 1 << 7
    SHUTDOWN_REQUEST = 1 << 8
    BLE_HOST_CONNECTED = 1 << 9
    BATTERY_HIGH_TEMP_SHUTDOWN = 1 << 10
    BATTERY_HIGH_TEMP_WARNING = 1 << 11
    USB_HOST_CONNECTED = 1 << 12
    FILE_IO_IN_PROGRESS = 1 << 13


NO_STATUS: Final = StatusFlag(0)
"""No status bits set."""


BATTERY_ALERT_FLAGS: Final = (
    StatusFlag.BATTERY_LOW_VOLTAGE_WARNING
    | StatusFlag.BATTERY_LOW_VOLTAGE_SHUTDOWN
    | StatusFlag.BATTERY_HIGH_CURRENT
    | StatusFlag.BATTERY_HIGH_TEMP_WARNING
    | StatusFlag.BATTERY_HIGH_TEMP_SHUTDOWN
)
"""Conditions worth telling the user about unprompted."""


class FeatureFlag(IntFlag):
    """Optional hub features, read from the capabilities characteristic."""

    BUILTIN_USER_PROGRAM_REPL = 1 << 0
    USER_PROG_FORMAT_MULTI_MPY_V6 = 1 << 1
    USER_PROG_FORMAT_MULTI_MPY_V6_1_NATIVE = 1 << 2
    BUILTIN_USER_PROGRAM_PORT_VIEW = 1 << 3
    BUILTIN_USER_PROGRAM_IMU_CALIBRATION = 1 << 4
    USER_PROG_FORMAT_MULTI_MPY_V6_3_NATIVE = 1 << 5


class UserProgramId(IntEnum):
    """Identifies which program to start."""

    FIRST_SLOT = 0
    LAST_SLOT = 127
    REPL = 128
    PORT_VIEW = 129
    IMU_CALIBRATION = 130
    # 131 and 132 are unassigned, so the gap is the firmware's, not a typo.
    EV3_MOTOR_BUTTON_CONTROL = 133
    EV3_MOTOR_IR_CONTROL = 134
    EV3_PORT_VIEW = 135


class HubError(IntEnum):
    """Error codes the hub returns when rejecting a command."""

    OK = 0
    INVALID_HANDLE = 0x01
    UNLIKELY_ERROR = 0x0E
    VALUE_NOT_ALLOWED = 0x13
    INVALID_COMMAND = 0x80
    BUSY = 0x81


# ------------------------------------------------------------------- payloads

WRITE_USER_RAM_HEADER_SIZE: Final = 5
"""One command byte plus a 32-bit offset."""

WRITE_APP_DATA_HEADER_SIZE: Final = 3
"""One command byte plus a *16-bit* offset. Narrower than user RAM."""

WRITE_STDIN_HEADER_SIZE: Final = 1


def encode_stop_user_program() -> bytes:
    """Build a command that stops the running program."""
    return struct.pack("<B", Command.STOP_USER_PROGRAM)


def start_takes_program_id(profile: Version) -> bool:
    """Whether this hub accepts the program to start being named.

    Args:
        profile: The hub's Pybricks profile version.

    Returns:
        True from profile v1.4.0, where ``START_USER_PROGRAM`` gained its
        argument. Naming the program is preferable to relying on the hub's
        own selection, which the profile documents as deprecated.
    """
    return profile >= _PROFILE_SLOT_ARGUMENT


def encode_start_user_program(program_id: int | None, *, profile: Version) -> bytes:
    """Build a command that starts a program already on the hub.

    Args:
        program_id: Slot number, or a :class:`UserProgramId`. ``None`` starts
            whichever program the hub currently has selected.
        profile: The hub's Pybricks profile version.

    Returns:
        The bytes to write.

    Raises:
        ValueError: If a specific program is requested from a hub too old to
            accept one, since silently starting the wrong program would be
            worse than failing.
    """

    if profile < _PROFILE_SLOT_ARGUMENT:
        if program_id is not None:
            raise ValueError(
                f"selecting a program needs profile "
                f"{'.'.join(map(str, _PROFILE_SLOT_ARGUMENT))} or later; "
                f"this hub reports {'.'.join(map(str, profile))}"
            )

        return struct.pack("<B", Command.START_USER_PROGRAM)

    if program_id is None:
        # Leaving the argument off still means "whichever slot is selected":
        # the firmware reads a one-byte command as exactly that. The Pybricks
        # Profile documents the form as removed in v1.4.0, though, so callers
        # are expected to name the program and this stays for the hubs that
        # have nothing else.
        return struct.pack("<B", Command.START_USER_PROGRAM)

    return struct.pack("<BB", Command.START_USER_PROGRAM, program_id)


def encode_start_repl(*, profile: Version) -> bytes:
    """Build a command that starts the interactive prompt.

    Args:
        profile: The hub's Pybricks profile version.

    Returns:
        The bytes to write. On profile v1.4.0 and later this is
        ``START_USER_PROGRAM`` with the REPL id, because the dedicated
        ``START_REPL`` command was removed.
    """

    if profile >= _PROFILE_SLOT_ARGUMENT:
        return struct.pack("<BB", Command.START_USER_PROGRAM, UserProgramId.REPL)

    return struct.pack("<B", Command.START_REPL)


def encode_write_user_program_meta(size: int) -> bytes:
    """Build a command that sets the stored program's length.

    Writing zero first invalidates whatever is on the hub, so an interrupted
    download can never leave a partly written program that looks runnable.

    Args:
        size: Program length in bytes, or zero to invalidate.

    Returns:
        The bytes to write.
    """
    return struct.pack("<BI", Command.WRITE_USER_PROGRAM_META, size)


def encode_write_user_ram(offset: int, payload: bytes) -> bytes:
    """Build a command that writes part of a program into hub memory.

    Args:
        offset: Byte offset from the start of the program.
        payload: The chunk to write.

    Returns:
        The bytes to write.
    """
    return struct.pack(f"<BI{len(payload)}s", Command.WRITE_USER_RAM, offset, payload)


def encode_reboot_to_update_mode() -> bytes:
    """Build a command that reboots the hub into firmware update mode."""
    return struct.pack("<B", Command.REBOOT_TO_UPDATE_MODE)


def encode_write_stdin(payload: bytes, *, profile: Version) -> bytes:
    """Build a command that sends data to the program's standard input.

    Args:
        payload: The bytes to send.
        profile: The hub's Pybricks profile version.

    Returns:
        The bytes to write.

    Raises:
        ValueError: If the hub predates standard input support.
    """

    if profile < (1, 3, 0):
        raise ValueError("this hub's firmware does not support writing to stdin")

    return struct.pack(f"<B{len(payload)}s", Command.WRITE_STDIN, payload)


def encode_write_app_data(offset: int, payload: bytes, *, profile: Version) -> bytes:
    """Build a command that writes into the program's app-data buffer.

    Unlike standard input this is not queued: it overwrites from ``offset``, and
    the program may read the buffer at any moment, so a sequence of writes must
    leave it valid at every step.

    Args:
        offset: Byte offset into the buffer. Sixteen bits, unlike user RAM.
        payload: The bytes to write.
        profile: The hub's Pybricks profile version.

    Returns:
        The bytes to write.

    Raises:
        ValueError: If the hub predates app data, or the offset does not fit.
    """

    if profile < (1, 4, 0):
        raise ValueError("this hub's firmware does not support app data")

    if not 0 <= offset <= 0xFFFF:
        raise ValueError(f"app data offset must fit in 16 bits, got {offset}")

    return struct.pack(f"<BH{len(payload)}s", Command.WRITE_APP_DATA, offset, payload)


# -------------------------------------------------------------------- parsing


@dataclass(frozen=True, slots=True)
class StatusReport:
    """A decoded status report."""

    flags: StatusFlag
    running_program_id: int | None
    """The running program, or ``None`` on profiles before v1.4.0."""

    selected_slot: int | None
    """The selected slot, or ``None`` on profiles before v1.5.0."""

    @property
    def program_running(self) -> bool:
        """Whether a user program is running."""
        return bool(self.flags & StatusFlag.USER_PROGRAM_RUNNING)


def parse_status_report(payload: bytes) -> StatusReport:
    """Decode a status report event.

    The trailing fields were added over time, so their absence is normal on
    older firmware and is reported as ``None`` rather than guessed at.

    Args:
        payload: The full event payload, including the leading event byte.

    Returns:
        The decoded report.

    Raises:
        ValueError: If the payload is too short to contain the flags.
    """

    if len(payload) < 5:
        raise ValueError(f"status report too short: {len(payload)} bytes")

    (flags,) = struct.unpack_from("<I", payload, 1)
    running = payload[5] if len(payload) >= 6 else None
    slot = payload[6] if len(payload) >= 7 else None
    return StatusReport(StatusFlag(flags), running, slot)


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a hub reports it can do."""

    max_write_size: int
    """Largest single write to the command characteristic."""

    flags: FeatureFlag
    max_program_size: int
    num_slots: int
    """Zero on profiles before v1.5.0, which had no slots."""

    @property
    def max_user_ram_payload(self) -> int:
        """Largest program chunk that fits in one WRITE_USER_RAM command."""
        return self.max_write_size - WRITE_USER_RAM_HEADER_SIZE

    @property
    def supports_multi_file(self) -> bool:
        """Whether the hub accepts the multi-module program format.

        All three multi-file feature flags count. Checking only the older two
        wrongly rejects hubs that advertise just the v6.3 native format, which
        is a real bug in the tool this replaces.
        """
        return bool(
            self.flags
            & (
                FeatureFlag.USER_PROG_FORMAT_MULTI_MPY_V6
                | FeatureFlag.USER_PROG_FORMAT_MULTI_MPY_V6_1_NATIVE
                | FeatureFlag.USER_PROG_FORMAT_MULTI_MPY_V6_3_NATIVE
            )
        )


def unpack_capabilities(data: bytes) -> Capabilities:
    """Decode the hub capabilities characteristic.

    Args:
        data: The raw characteristic value. Eleven bytes on profile v1.5.0 and
            later, ten before that.

    Returns:
        The decoded capabilities.

    Raises:
        ValueError: If the value is not a length this code understands.
    """

    if len(data) >= 11:
        max_write, flags, max_program, slots = struct.unpack_from("<HIIB", data, 0)
    elif len(data) >= 10:
        max_write, flags, max_program = struct.unpack_from("<HII", data, 0)
        slots = 0
    else:
        raise ValueError(f"hub capabilities too short: {len(data)} bytes")

    return Capabilities(max_write, FeatureFlag(flags), max_program, slots)


@dataclass(frozen=True, slots=True)
class PnpId:
    """The Plug and Play identifier, which says what kind of hub this is."""

    vendor_id_source: int
    vendor_id: int
    product_id: int
    product_version: int


def unpack_pnp_id(data: bytes) -> PnpId:
    """Decode the Plug and Play ID characteristic.

    Args:
        data: The raw characteristic value.

    Returns:
        The decoded identifier.

    Raises:
        ValueError: If the value is not seven bytes.
    """

    if len(data) != 7:
        raise ValueError(f"PnP ID must be 7 bytes, got {len(data)}")

    source, vendor, product, version = struct.unpack("<BHHH", data)
    return PnpId(source, vendor, product, version)


class HubType(IntEnum):
    """Product ids LEGO assigns to its hubs, as carried in the PnP ID.

    These are the LWP3 hub kinds. Only the ones that can run Pybricks are
    listed; the numbering is LEGO's and is not contiguous.
    """

    MOVE_HUB = 0x40
    CITY_HUB = 0x41
    TECHNIC_HUB = 0x80
    TECHNIC_LARGE_HUB = 0x81
    """SPIKE Prime and MINDSTORMS Robot Inventor, told apart by the variant."""
    TECHNIC_SMALL_HUB = 0x83
    """SPIKE Essential."""
    TECHNIC_MOVE_HUB = 0x84
    NXT = 0xE1
    """MINDSTORMS NXT."""
    EV3 = 0xE2
    """MINDSTORMS EV3.

    Reached over USB rather than Bluetooth, which is why this was missed until
    a real EV3 running Pybricks reported a product id nothing here knew.
    """


class VendorIdSource(IntEnum):
    """Who assigned the vendor id in a PnP ID."""

    BLUETOOTH_SIG = 1
    USB_IMPLEMENTERS_FORUM = 2


_HUB_TYPE_NAMES: Final[Mapping[tuple[int, int | None], str]] = {
    (HubType.MOVE_HUB, None): "Move hub",
    (HubType.CITY_HUB, None): "City hub",
    (HubType.TECHNIC_HUB, None): "Technic hub",
    (HubType.TECHNIC_MOVE_HUB, None): "Technic Move hub",
    # The large and small Technic hubs are each sold under more than one name,
    # and the product version is what says which.
    (HubType.TECHNIC_LARGE_HUB, 0): "Prime hub",
    (HubType.TECHNIC_LARGE_HUB, 1): "Inventor hub",
    # The same Prime hub, built on an STM32H5 instead of an STM32F4. The
    # firmware hard-codes this variant for the prime_hub_h5 platform, where
    # the F4 build reads 0 or 1 out of the stock bootloader's flash instead.
    # It is the same product to whoever bought it, so it gets the same name;
    # the two need telling apart when installing firmware, not here.
    (HubType.TECHNIC_LARGE_HUB, 2): "Prime hub",
    (HubType.TECHNIC_SMALL_HUB, 0): "Essential hub",
    # The two MINDSTORMS bricks, which are "bricks" rather than "hubs" in
    # LEGO's own vocabulary and in the firmware's comments.
    (HubType.NXT, None): "NXT brick",
    (HubType.EV3, None): "EV3 brick",
}
"""What to call each hub, following Pybricks Code so that the two agree.

Keyed by product id and, where a hub has variants, product version. A hub
whose variant is unknown falls back to the entry with no variant, if there is
one.
"""


def hub_type_name(pnp: PnpId | None) -> str | None:
    """Name the kind of hub a PnP ID describes.

    Args:
        pnp: The decoded PnP ID, or ``None`` if the hub did not give one.

    Returns:
        A name to show a user, such as ``"Technic hub"``, or ``None`` when
        this is not a LEGO hub we know about. Callers say nothing rather than
        guessing: a hub that works but cannot be named is better described by
        silence than by "Unsupported".
    """

    if pnp is None:
        return None

    if pnp.vendor_id_source != VendorIdSource.BLUETOOTH_SIG:
        return None

    if pnp.vendor_id != LEGO_COMPANY_ID:
        return None

    exact = _HUB_TYPE_NAMES.get((pnp.product_id, pnp.product_version))
    return exact or _HUB_TYPE_NAMES.get((pnp.product_id, None))
