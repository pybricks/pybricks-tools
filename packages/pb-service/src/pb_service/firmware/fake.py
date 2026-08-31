# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""A simulated web, so that firmware can be exercised without one.

The counterpart of :mod:`pb_service.hub.fake`, and there for the same reasons.
A test suite that reaches GitHub fails on an aeroplane, spends the sixty
requests an hour an unauthenticated caller is allowed, and cannot make the
server return a truncated archive or refuse to answer at all -- which are the
cases worth testing.

The archives built here are real archives: proper zip files with proper
metadata, small enough to keep in memory. What is fake is where they came
from.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Final, final

from pb_service.firmware.catalogue import ASSET_NAMES
from pb_service.firmware.fetch import Progress

__all__ = ["FakeFetcher", "release_listing", "tiny_firmware"]

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
