# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""A simulated web and a simulated bootloader, so neither is needed for real.

The counterpart of :mod:`pb_service.hub.fake`, and there for the same reasons.
A test suite that reaches GitHub fails on an aeroplane, spends the sixty
requests an hour an unauthenticated caller is allowed, and cannot make the
server return a truncated archive or refuse to answer at all -- which are the
cases worth testing.

The archives built here are real archives: proper zip files with proper
metadata, small enough to keep in memory. What is fake is where they came from.
The bootloader is the same idea -- it speaks the real wire format, so the code
under test is the code that talks to hardware; what it stands in for is the
hub, not the protocol.
"""

from __future__ import annotations

import json
import struct
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Final, final

from pb_service.firmware.bootloader import lwp3
from pb_service.firmware.catalogue import ASSET_NAMES
from pb_service.firmware.fetch import Progress

__all__ = ["FakeBootloader", "FakeFetcher", "release_listing", "tiny_firmware"]

RELEASES_HOST: Final = "https://github.com/pybricks/pybricks-micropython/releases"
"""Where the real assets live, so the fake URLs look like the real ones."""

_PLATFORMS: Final[Mapping[int, tuple[str, ...]]] = {
    0x40: ("move_hub",),
    0x41: ("city_hub",),
    0x80: ("technic_hub",),
    # The one that matters: a single archive holding two builds, which is why
    # a variant has to be chosen from the hardware rather than the hub type.
    0x81: ("prime_hub_f4", "prime_hub_h5"),
    0x83: ("essential_hub",),
    0xE1: ("nxt",),
    0xE2: ("ev3",),
}
"""Which pbio platforms each hub type's archive carries."""


def tiny_firmware(
    hub_type: int,
    version: str,
    *,
    name_size: int = 16,
    body: bytes = b"",
) -> bytes:
    """Build a small but genuine firmware archive.

    Args:
        hub_type: The ``device-id`` it declares.
        version: The firmware version it declares.
        name_size: How much room to reserve for a hub name, or zero for none,
            which is what every official LEGO image does.
        body: The image contents, padded out to hold the name field.

    Returns:
        A zip file that :func:`pb_service.firmware.image.read_firmware` reads.
    """
    platforms = _PLATFORMS.get(hub_type, ("unknown",))
    image = bytearray(body or bytes(64))
    if name_size:
        image.extend(bytes(max(0, 64 + name_size - len(image))))

    variants: list[dict[str, Any]] = []
    members: dict[str, bytes] = {}
    for platform in platforms:
        member = f"firmware-base-{platform}.bin"
        # Each platform gets a different image, so a test can tell which one
        # was chosen rather than only that something was.
        members[member] = bytes(image) + platform.encode().ljust(16, b"\0")
        variants.append(
            {
                "platform": platform,
                "firmware": member,
                "checksum-type": "none",
                "checksum-size": 0,
                "hub-name-offset": 64 if name_size else 0,
                "hub-name-size": name_size,
            }
        )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "firmware.metadata.json",
            json.dumps(
                {
                    "metadata-version": "3.0.0",
                    "firmware-version": version,
                    "device-id": hub_type,
                    "variants": variants,
                }
            ),
        )
        for member, data in members.items():
            archive.writestr(member, data)
        archive.writestr("ReadMe_OSS.txt", b"Pretend notices.")
    return buffer.getvalue()


def release_listing(*versions: tuple[str, bool]) -> bytes:
    """Build what GitHub's releases API would have answered.

    Args:
        *versions: Each version and whether it is a prerelease, newest first.

    Returns:
        The JSON body.
    """
    releases: list[dict[str, Any]] = []
    for version, prerelease in versions:
        tag = f"v{version}"
        releases.append(
            {
                "tag_name": tag,
                "prerelease": prerelease,
                "draft": False,
                "published_at": "2026-08-27T09:50:30Z",
                "assets": [
                    {
                        "name": f"pybricks-{hub}-{tag}.zip",
                        "browser_download_url": f"{RELEASES_HOST}/download/{tag}"
                        f"/pybricks-{hub}-{tag}.zip",
                    }
                    for hub in ASSET_NAMES.values()
                ],
            }
        )
    return json.dumps(releases).encode()


@final
@dataclass
class FakeFetcher:
    """Answers with whatever it has been given, and counts the asking.

    The count is the point of it being a class: how often the release listing
    is fetched is a property worth asserting, since it is what keeps a session
    inside GitHub's rate limit.
    """

    bodies: dict[str, bytes] = field(default_factory=dict[str, bytes])
    """What to answer with, by URL."""

    failures: dict[str, str] = field(default_factory=dict[str, str])
    """URLs that should fail, and what to say when they do."""

    requested: list[str] = field(default_factory=list[str])
    """Every URL asked for, in order."""

    async def fetch(self, url: str, *, on_progress: Progress | None = None) -> bytes:
        """Answer a request.

        Args:
            url: What was asked for.
            on_progress: Called once with the whole size, so that a caller
                drawing a bar is exercised rather than merely present.

        Returns:
            The body.

        Raises:
            OSError: If this URL was set up to fail, or was never set up. An
                unknown URL failing is deliberate: a test that fetches
                something it did not arrange should hear about it.
        """
        self.requested.append(url)
        if url in self.failures:
            raise OSError(self.failures[url])
        body = self.bodies.get(url)
        if body is None:
            raise OSError(f"nothing arranged for {url}")
        if on_progress is not None:
            on_progress(len(body), len(body))
        return body

    def count(self, url: str) -> int:
        """Return how many times a URL has been asked for.

        Args:
            url: The URL.

        Returns:
            The number of requests.
        """
        return self.requested.count(url)


@final
@dataclass
class FakeBootloader:
    """A simulated LEGO bootloader, sitting where a hub would be.

    Speaks the real wire format, so the code under test is the code that talks
    to hardware. What it stands in for is the hub, not the protocol.

    The interesting settings are the ways it misbehaves. The City hub's
    bootloader answers an erase even when told not to reply, and sometimes the
    reply never arrives at all; both are worked around in the flasher, and a
    workaround nothing exercises is a workaround that quietly stops working.
    """

    hub_type: int = 0x41
    start_address: int = 0x08005000
    end_address: int = 0x0801FFFF
    version: int = 7

    answer_erase: bool = True
    """Whether the erase is answered. The City hub sometimes does not."""

    report_count_as: int | None = None
    """Report this many bytes taken, rather than the true number.

    For the case that matters most and is otherwise unreachable: a hub that
    accepted the writes but did not end up with all of them. The one
    acknowledgement in the whole flash is what says so.
    """

    written: bytearray = field(default_factory=bytearray)
    """Everything that was programmed, in the order it arrived."""

    writes: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    """Where each write said it should go, and how much it carried.

    Kept because firmware written to the wrong addresses can still add up to
    the right number of bytes, so the total is not enough to check.
    """

    declared_size: int | None = None
    """What the loader was told to expect, if it has been initialised."""

    erased: bool = False
    started: bool = False
    """Whether the firmware was told to run, which is the end of a flash."""

    _handler: Callable[[bytes], None] | None = None

    def notify(self, payload: bytes) -> None:
        """Send a notification back, if anybody is listening."""
        if self._handler is not None:
            self._handler(payload)

    def handle(self, request: bytes) -> None:
        """Act on one write to the bootloader characteristic.

        Args:
            request: What was written.

        Raises:
            ValueError: If the request is empty, which the wire cannot carry.
        """
        if not request:
            raise ValueError("an empty bootloader request")

        command = request[0]
        if command == lwp3.BootloaderCommand.GET_INFO:
            self.notify(
                bytes([command])
                + struct.pack(
                    "<iIIB",
                    self.version,
                    self.start_address,
                    self.end_address,
                    self.hub_type,
                )
            )
        elif command == lwp3.BootloaderCommand.ERASE_FLASH:
            self.erased = True
            self.written.clear()
            self.writes.clear()
            if self.answer_erase:
                self.notify(bytes([command, 0x00]))
        elif command == lwp3.BootloaderCommand.INIT_LOADER:
            (self.declared_size,) = struct.unpack_from("<I", request, 1)
            self.notify(bytes([command, 0x00]))
        elif command == lwp3.BootloaderCommand.PROGRAM_FLASH:
            self._program(request)
        elif command == lwp3.BootloaderCommand.GET_CHECKSUM:
            self.notify(bytes([command, self._checksum()]))
        elif command == lwp3.BootloaderCommand.START_APP:
            # No reply: the real one reboots, taking Bluetooth with it.
            self.started = True
        else:
            raise ValueError(f"an unknown bootloader command: {command:#04x}")

    def _program(self, request: bytes) -> None:
        """Take one write of firmware, answering only the last."""
        (address,) = struct.unpack_from("<I", request, 2)
        payload = request[6:]
        self.writes.append((address, len(payload)))
        self.written.extend(payload)
        if self.declared_size is not None and len(self.written) >= self.declared_size:
            count = (
                len(self.written)
                if self.report_count_as is None
                else self.report_count_as
            )
            self.notify(
                bytes([lwp3.BootloaderCommand.PROGRAM_FLASH])
                + struct.pack("<BI", self._checksum(), count)
            )

    def _checksum(self) -> int:
        """Exclusive-or every byte written so far, as the real one does."""
        value = 0xFF
        for byte in self.written:
            value ^= byte
        return value

    @property
    def contiguous(self) -> bool:
        """Whether the writes covered the range without gaps or overlaps.

        Worth checking after a flash on its own, because firmware written to
        the wrong addresses still adds up to the right number of bytes.

        Returns:
            Whether every write began where the previous one ended, starting
            at this bootloader's own start address.
        """
        expected = self.start_address
        for address, length in self.writes:
            if address != expected:
                return False
            expected += length
        return True
