# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""A simulated hub, used by the tests and by ``PB_HUB_BACKEND=fake``.

This models the hub's GATT surface rather than stubbing out the service's calls,
so the protocol code under test is the same code that talks to real hardware.
It can be told to behave like any profile version between v1.2.0 and v1.5.0,
to reject commands the way firmware does, and to disconnect at an awkward
moment, because those are the cases that are otherwise only reachable with a
hub, a stopwatch and some luck.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import final

import anyio

from pb_service.hub import protocol as p
from pb_service.hub.transport import (
    DeviceInfo,
    HubCommandError,
    HubLink,
    NotificationHandler,
    ScanFilter,
    UnavailableDevice,
)

logger = logging.getLogger(__name__)

__all__ = ["FakeHub", "FakeHubTransport"]


class HubRejection(HubCommandError):
    """The hub refused a command, as an ATT error would.

    A :class:`HubCommandError`, because a simulated refusal that the service
    handled differently from a real one would be worth nothing.
    """

    def __init__(self, error: p.HubError) -> None:
        """Record which error the hub returned.

        Args:
            error: The firmware's error code.
        """
        super().__init__(error, f"hub rejected the command: {error.name}")


@dataclass
class FakeHub:
    """The state and behaviour of one simulated hub."""

    name: str = "Fake Hub"
    address: str = "AA:BB:CC:DD:EE:FF"
    profile: p.Version = (1, 5, 0)
    firmware: str = "4.0.1"
    hub_type: int = p.HubType.TECHNIC_LARGE_HUB
    variant: int = 0
    """Product id and version, as the PnP ID reports them."""

    max_write_size: int = 512
    max_program_size: int = 16384
    num_slots: int = 5
    features: p.FeatureFlag = (
        p.FeatureFlag.BUILTIN_USER_PROGRAM_REPL
        | p.FeatureFlag.USER_PROG_FORMAT_MULTI_MPY_V6
    )

    status: p.StatusFlag = p.NO_STATUS
    running_program_id: int = 0
    selected_slot: int = 0

    program: bytearray = field(default_factory=bytearray)
    program_size: int = 0
    """The declared length. Zero means nothing runnable is stored."""

    stdin: bytearray = field(default_factory=bytearray)
    stdin_writes: list[bytes] = field(default_factory=list[bytes])
    """Each write of input this hub accepted, so a test can see how it was cut."""

    stdin_capacity: int = 0
    """How much unread input this hub can hold, or zero for no limit.

    Real firmware refuses a write that does not fit whole, and how much fits
    depends on the build -- older ones hold as little as 21 bytes. Nothing
    consumes the buffer here, so a test empties it to stand for a program
    reading its input.
    """

    app_data: bytearray = field(default_factory=lambda: bytearray(64))

    reject_next: p.HubError | None = None
    """Make the next command fail, to exercise error mapping."""

    commands: list[bytes] = field(default_factory=list[bytes])
    """Every command received, so tests can assert on the wire traffic."""

    on_status: Callable[[], None] | None = None
    """Called after each status report, so tests can wait rather than poll."""

    on_disconnect: Callable[[], None] | None = None
    """Called when this hub drops, as the radio reports a real one going."""

    _handler: NotificationHandler | None = None
    _connected: bool = False

    # ------------------------------------------------------------- simulation

    @property
    def device_id(self) -> str:
        """The hub's service-facing identifier."""
        return f"fake:{self.address}"

    def notify(self, payload: bytes) -> None:
        """Send a notification to the connected client, if any."""

        if self._handler is not None:
            self._handler(payload)

    def send_status(self) -> None:
        """Send a status report shaped for this hub's profile version."""
        payload = bytearray([p.Event.STATUS_REPORT])
        payload += struct.pack("<I", int(self.status))

        if self.profile >= (1, 4, 0):
            payload.append(self.running_program_id)

        if self.profile >= (1, 5, 0):
            payload.append(self.selected_slot)

        self.notify(bytes(payload))

        if self.on_status is not None:
            self.on_status()

    def send_stdout(self, data: bytes) -> None:
        """Send program output."""
        self.notify(bytes([p.Event.WRITE_STDOUT]) + data)

    def send_app_data(self, data: bytes) -> None:
        """Send app data."""
        self.notify(bytes([p.Event.WRITE_APP_DATA]) + data)

    def set_program_running(self, running: bool) -> None:
        """Start or stop the simulated program and report the change."""

        if running:
            self.status |= p.StatusFlag.USER_PROGRAM_RUNNING
        else:
            self.status &= ~p.StatusFlag.USER_PROGRAM_RUNNING

        self.send_status()

    def press_power_button(self, pressed: bool = True) -> None:
        """Simulate the power button being held or released."""

        if pressed:
            self.status |= p.StatusFlag.POWER_BUTTON_PRESSED
        else:
            self.status &= ~p.StatusFlag.POWER_BUTTON_PRESSED

        self.send_status()

    def disconnect(self) -> None:
        """Drop the connection, as a hub going out of range would."""
        logger.debug("fake hub: dropping the connection")
        self._connected = False
        self._handler = None
        if self.on_disconnect is not None:
            # A real transport reports this, so this one has to as well, or
            # the service would go on believing the hub is there.
            self.on_disconnect()

    # ---------------------------------------------------------- command entry

    def handle_command(self, data: bytes) -> None:
        """Apply one command written to the command characteristic.

        Args:
            data: The raw command bytes.

        Raises:
            HubRejection: If the hub refuses, as real firmware would.
        """
        self.commands.append(bytes(data))

        if self.reject_next is not None:
            error, self.reject_next = self.reject_next, None
            raise HubRejection(error)

        if not data:
            raise HubRejection(p.HubError.VALUE_NOT_ALLOWED)

        command = data[0]

        if command == p.Command.STOP_USER_PROGRAM:
            self.set_program_running(False)
        elif command == p.Command.START_USER_PROGRAM:
            self._start(data)
        elif command == p.Command.START_REPL:
            if self.profile >= (1, 4, 0):
                # Removed in v1.4.0, so a hub of this vintage would not know it.
                raise HubRejection(p.HubError.INVALID_COMMAND)

            self.running_program_id = p.UserProgramId.REPL
            self.set_program_running(True)
        elif command == p.Command.WRITE_USER_PROGRAM_META:
            (self.program_size,) = struct.unpack_from("<I", data, 1)
            if self.program_size == 0:
                self.program.clear()
        elif command == p.Command.WRITE_USER_RAM:
            self._write_ram(data)
        elif command == p.Command.WRITE_STDIN:
            if self.profile < (1, 3, 0):
                raise HubRejection(p.HubError.INVALID_COMMAND)

            payload = data[1:]
            if self.stdin_capacity and len(payload) > self.stdin_capacity - len(
                self.stdin
            ):
                # All or nothing, exactly as the firmware does it: what does
                # not fit is refused rather than partly kept.
                raise HubRejection(p.HubError.BUSY)

            self.stdin += payload
            self.stdin_writes.append(bytes(payload))
        elif command == p.Command.WRITE_APP_DATA:
            self._write_app_data(data)
        elif command == p.Command.REBOOT_TO_UPDATE_MODE:
            self.disconnect()
        else:
            raise HubRejection(p.HubError.INVALID_COMMAND)

    def _start(self, data: bytes) -> None:
        if self.status & p.StatusFlag.USER_PROGRAM_RUNNING:
            raise HubRejection(p.HubError.BUSY)

        if len(data) > 1:
            if self.profile < (1, 4, 0):
                raise HubRejection(p.HubError.VALUE_NOT_ALLOWED)

            self.running_program_id = data[1]
        else:
            self.running_program_id = self.selected_slot

        if self.running_program_id < p.UserProgramId.REPL and self.program_size == 0:
            # Nothing was ever downloaded, so there is nothing to run.
            raise HubRejection(p.HubError.VALUE_NOT_ALLOWED)

        self.set_program_running(True)

    def _write_ram(self, data: bytes) -> None:
        (offset,) = struct.unpack_from("<I", data, 1)
        payload = data[p.WRITE_USER_RAM_HEADER_SIZE :]
        end = offset + len(payload)
        if end > self.max_program_size:
            raise HubRejection(p.HubError.VALUE_NOT_ALLOWED)

        if len(self.program) < end:
            self.program.extend(b"\x00" * (end - len(self.program)))

        self.program[offset:end] = payload

    def _write_app_data(self, data: bytes) -> None:
        if self.profile < (1, 4, 0):
            raise HubRejection(p.HubError.INVALID_COMMAND)

        (offset,) = struct.unpack_from("<H", data, 1)
        payload = data[p.WRITE_APP_DATA_HEADER_SIZE :]
        end = offset + len(payload)
        if end > len(self.app_data):
            raise HubRejection(p.HubError.VALUE_NOT_ALLOWED)

        self.app_data[offset:end] = payload

    # ------------------------------------------------------- characteristics

    def read(self, uuid: str) -> bytes:
        """Return a characteristic value.

        Args:
            uuid: The characteristic to read.

        Returns:
            Its value.

        Raises:
            HubRejection: If this hub has no such characteristic.
        """

        if uuid == p.DEVICE_NAME_UUID:
            return self.name.encode()

        if uuid == p.FIRMWARE_REVISION_UUID:
            return self.firmware.encode()

        if uuid == p.SOFTWARE_REVISION_UUID:
            return ".".join(str(part) for part in self.profile).encode()

        if uuid == p.PNP_ID_UUID:
            return struct.pack(
                "<BHHH",
                p.VendorIdSource.BLUETOOTH_SIG,
                p.LEGO_COMPANY_ID,
                self.hub_type,
                self.variant,
            )

        if uuid == p.PYBRICKS_HUB_CAPABILITIES_UUID:
            if self.profile >= (1, 5, 0):
                return struct.pack(
                    "<HIIB",
                    self.max_write_size,
                    int(self.features),
                    self.max_program_size,
                    self.num_slots,
                )

            return struct.pack(
                "<HII", self.max_write_size, int(self.features), self.max_program_size
            )

        raise HubRejection(p.HubError.INVALID_HANDLE)


@final
class _FakeLink(HubLink):
    """An open connection to a :class:`FakeHub`."""

    def __init__(self, hub: FakeHub) -> None:
        self._hub = hub

    async def read_characteristic(self, uuid: str) -> bytes:
        self._require_connected()
        return self._hub.read(uuid)

    async def read_name(self) -> str:
        self._require_connected()
        return self._hub.name

    async def write_characteristic(
        self, uuid: str, data: bytes, *, response: bool = True
    ) -> None:
        self._require_connected()

        if uuid != p.PYBRICKS_COMMAND_EVENT_UUID:
            raise HubRejection(p.HubError.INVALID_HANDLE)

        self._hub.handle_command(data)

    async def start_notify(self, uuid: str, handler: NotificationHandler) -> None:
        self._require_connected()

        if uuid != p.PYBRICKS_COMMAND_EVENT_UUID:
            raise HubRejection(p.HubError.INVALID_HANDLE)

        self._hub._handler = handler  # pyright: ignore[reportPrivateUsage]

    @property
    def is_connected(self) -> bool:
        return self._hub._connected  # pyright: ignore[reportPrivateUsage]

    def _require_connected(self) -> None:
        if not self.is_connected:
            raise ConnectionResetError("the fake hub is not connected")


@final
class FakeHubTransport:
    """Serves a fixed set of simulated hubs."""

    def __init__(self, *hubs: FakeHub) -> None:
        """Create a transport.

        Args:
            *hubs: The hubs it should report. Defaults to a single hub.
        """
        self.hubs = list(hubs) if hubs else [FakeHub()]
        self.scan_delay = 0.0
        """Seconds a scan should appear to take, for testing cancellation."""

        self.on_disconnect: Callable[[str], None] | None = None
        """Set by whoever owns the connections, and called when a hub drops."""

        self._unavailable: tuple[UnavailableDevice, ...] = ()

        for hub in self.hubs:
            hub.on_disconnect = self._dropped(hub)

    def _dropped(self, hub: FakeHub) -> Callable[[], None]:
        """Build the callback one hub uses to report that it has gone.

        Args:
            hub: The hub.

        Returns:
            What to call when it disconnects.
        """

        def dropped() -> None:
            if self.on_disconnect is not None:
                self.on_disconnect(hub.device_id)

        return dropped

    @property
    def scheme(self) -> str:
        """Identifier prefix for simulated hubs."""
        return "fake"

    @property
    def unavailable(self) -> tuple[UnavailableDevice, ...]:
        """Whatever a test has said is there but out of reach.

        Empty unless something assigns to it, since a simulated hub has no
        device node to be refused, and it is settable so that a test can put
        the service in the state a locked-down serial port produces without
        needing one.
        """
        return self._unavailable

    @unavailable.setter
    def unavailable(self, devices: tuple[UnavailableDevice, ...]) -> None:
        self._unavailable = devices

    async def discover(
        self, scan_filter: ScanFilter
    ) -> AsyncGenerator[DeviceInfo, None]:
        """Report the simulated hubs that match, then wait to be stopped.

        Args:
            scan_filter: Which hubs to report.

        Yields:
            The matching hubs.
        """

        if self.scan_delay:
            await anyio.sleep(self.scan_delay)

        for hub in self.hubs:
            if scan_filter.name is not None and hub.name != scan_filter.name:
                continue

            if hub.device_id in scan_filter.exclude:
                continue

            yield DeviceInfo(
                id=hub.device_id,
                name=hub.name,
                address=hub.address,
                rssi=-50,
                transport=self.scheme,
                # A real hub advertises its Plug and Play ID in the scan
                # response, so this one does too.
                hub_type=p.hub_type_name(
                    p.PnpId(
                        vendor_id_source=p.VendorIdSource.BLUETOOTH_SIG,
                        vendor_id=p.LEGO_COMPANY_ID,
                        product_id=hub.hub_type,
                        product_version=hub.variant,
                    )
                ),
            )

        # A real scan keeps running until the caller stops it, so this does
        # too; otherwise tests would not exercise the cancellation path.
        await anyio.sleep_forever()

    def open(self, device_id: str) -> AbstractAsyncContextManager[HubLink]:
        """Connect to a simulated hub.

        Args:
            device_id: The hub's identifier.

        Returns:
            An async context manager yielding the link.
        """
        return self._open(device_id)

    @asynccontextmanager
    async def _open(self, device_id: str) -> AsyncGenerator[HubLink, None]:
        for hub in self.hubs:
            if hub.device_id == device_id:
                break
        else:
            raise ConnectionRefusedError(f"no such hub: {device_id}")

        hub._connected = True  # pyright: ignore[reportPrivateUsage]
        logger.debug("fake hub: connected to %s", device_id)

        try:
            yield _FakeLink(hub)
        finally:
            hub.disconnect()
