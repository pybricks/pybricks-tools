# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What firmware exists, and which of it somebody meant.

Two jobs. Asking GitHub what has been released, which is a network call and is
cached because there are only sixty of those an hour to an unauthenticated
caller; and turning what a user typed into one of the answers, which is pure
and is where the interesting rules are.

Version requests are deliberately loose. ``4.1.0b3`` is exact, ``4.1`` is the
newest of those, and nothing at all is the newest there is. Prereleases are
left out unless asked for, except when one is named exactly -- refusing to
install the thing somebody literally typed would be pedantry rather than
safety.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

from packaging.version import InvalidVersion, Version

from pb_service.firmware.errors import (
    FirmwareBadArchiveError,
    FirmwareNotFoundError,
    FirmwareUnavailableError,
)
from pb_service.firmware.fetch import Fetcher

logger = logging.getLogger(__name__)

__all__ = [
    "ASSET_NAMES",
    "Catalogue",
    "Release",
    "asset_name_for",
    "choose",
]

RELEASES_URL: Final = (
    "https://api.github.com/repos/pybricks/pybricks-micropython/releases?per_page=50"
)
"""Where the published firmware is listed.

Fifty is generous -- a user picking a version is choosing from the last few --
and it keeps the whole history in one request rather than paging.
"""

INDEX_LIFETIME: Final = 15 * 60
"""Seconds a listing is reused for.

Long enough that flashing several hubs in a row is one request rather than
several, short enough that a release published this afternoon is found this
afternoon. ``--refresh`` is there for the minutes in between.
"""

ASSET_NAMES: Final[Mapping[int, str]] = {
    0x40: "movehub",
    0x41: "cityhub",
    0x80: "technichub",
    0x81: "primehub",
    0x83: "essentialhub",
    0xE1: "nxt",
    0xE2: "ev3",
}
"""How each hub type is spelled in a release asset's name.

Not the same vocabulary as the pbio platform names inside an archive -- this
is ``cityhub`` where that is ``city_hub``, and one ``primehub`` asset holds
two platforms. Two namespaces that look alike is exactly the sort of thing
that gets confused, so they are kept in the modules that own them.
"""


@dataclass(frozen=True, slots=True)
class Release:
    """One published set of firmware."""

    version: str
    """The version as a user would type it, such as ``4.1.0b3``."""

    tag: str
    """The tag it was released under, which is the version with a ``v``."""

    prerelease: bool
    """Whether GitHub marks it as a prerelease."""

    published: str
    """When it was published, as an ISO timestamp."""

    assets: Mapping[str, str] = field(default_factory=dict[str, str])
    """Where to get each hub's archive, by asset hub name."""

    def url_for(self, hub_type: int) -> str:
        """Return where to get this release's firmware for a hub.

        Args:
            hub_type: The ``device-id`` of the hub.

        Returns:
            The download URL.

        Raises:
            FirmwareNotFoundError: If this release has nothing for that hub,
                which is what an older release looks like for a hub that was
                not supported yet.
        """
        name = asset_name_for(hub_type)
        url = self.assets.get(name)
        if url is None:
            raise FirmwareNotFoundError(
                f"firmware {self.version} has nothing for a {name}"
            )
        return url


def asset_name_for(hub_type: int) -> str:
    """Return how a hub type is spelled in an asset name.

    Args:
        hub_type: The ``device-id`` of the hub.

    Returns:
        The name, such as ``cityhub``.

    Raises:
        FirmwareNotFoundError: If there is no Pybricks firmware for that hub.
    """
    name = ASSET_NAMES.get(hub_type)
    if name is None:
        raise FirmwareNotFoundError(
            f"no Pybricks firmware for hub type {hub_type:#04x}"
        )
    return name


def _version_of(tag: str) -> str:
    """Return the version a tag names, which is the tag without its ``v``."""
    return tag[1:] if tag.startswith("v") else tag


def _parsed(version: str) -> Version | None:
    """Return a comparable version, or ``None`` if it is not one we can read."""
    try:
        return Version(version)
    except InvalidVersion:
        return None


def _newest_first(releases: Sequence[Release]) -> tuple[Release, ...]:
    """Sort releases by version, newest first.

    Sorted by what the version means rather than by what GitHub happened to
    return, so that ``4.1.0b3`` sits after ``4.1.0b10`` rather than before it.
    Anything unparseable keeps its published order at the end.
    """
    known = [r for r in releases if _parsed(r.version) is not None]
    unknown = [r for r in releases if _parsed(r.version) is None]
    known.sort(key=lambda r: cast("Version", _parsed(r.version)), reverse=True)
    return (*known, *unknown)


def _matches(release: Release, request: str) -> bool:
    """Whether a release answers a version request.

    An exact version matches itself. A partial one -- ``4.1``, or ``4`` --
    matches anything below it, compared component by component rather than as
    text, so ``4.1`` does not match ``4.10``.
    """
    if release.version == request:
        return True
    wanted, got = _parsed(request), _parsed(release.version)
    if wanted is None or got is None:
        return False
    return got.release[: len(wanted.release)] == wanted.release and (
        wanted.pre is None or wanted.pre == got.pre
    )


def choose(
    releases: Sequence[Release], request: str | None, *, allow_prerelease: bool = False
) -> Release:
    """Pick the release a user meant.

    Args:
        releases: What is available.
        request: What was asked for -- an exact version, a partial one, or
            ``None`` for the newest.
        allow_prerelease: Whether betas may be chosen. Ignored when the
            request names one exactly, since that is not ambiguous.

    Returns:
        The release.

    Raises:
        FirmwareNotFoundError: If nothing matches.
    """
    ordered = _newest_first(releases)
    named_exactly = any(r.version == request for r in ordered)

    for release in ordered:
        if request is not None and not _matches(release, request):
            continue
        # An exact prerelease is what the user typed, so it is what they get.
        if release.prerelease and not allow_prerelease and not named_exactly:
            continue
        return release

    if not ordered:
        raise FirmwareNotFoundError("no released firmware was found")

    # Nothing matched, and the useful thing to say is whether that is because
    # there is no such version or because every one of them is a prerelease.
    # The second has a fix and the first does not, and they read identically
    # from the outside.
    only_prereleases = any(
        release.prerelease and (request is None or _matches(release, request))
        for release in ordered
    )
    if only_prereleases:
        described = f"firmware {request}" if request else "released firmware"
        raise FirmwareNotFoundError(f"the only {described} is a prerelease")
    if request is None:
        raise FirmwareNotFoundError("no released firmware was found")
    raise FirmwareNotFoundError(f"no firmware version matches {request}")


def _releases_from(document: object) -> tuple[Release, ...]:
    """Read GitHub's answer into releases, ignoring anything unexpected.

    Args:
        document: The decoded JSON.

    Returns:
        What it described.

    Raises:
        FirmwareBadArchiveError: If it is not the list of releases it should be.
    """
    if not isinstance(document, list):
        raise FirmwareBadArchiveError("the release listing is not a list")

    found: list[Release] = []
    for entry in cast("list[Any]", document):
        if not isinstance(entry, dict):
            continue
        release = cast("dict[str, Any]", entry)
        tag = release.get("tag_name")
        if not isinstance(tag, str) or release.get("draft"):
            continue
        assets: dict[str, str] = {}
        for item in cast("list[Any]", release.get("assets") or []):
            if not isinstance(item, dict):
                continue
            asset = cast("dict[str, Any]", item)
            name, url = asset.get("name"), asset.get("browser_download_url")
            if not isinstance(name, str) or not isinstance(url, str):
                continue
            # pybricks-cityhub-v4.1.0b3.zip -> cityhub
            parts = name.split("-")
            if len(parts) >= 3 and parts[0] == "pybricks" and name.endswith(".zip"):
                assets[parts[1]] = url
        found.append(
            Release(
                version=_version_of(tag),
                tag=tag,
                prerelease=bool(release.get("prerelease")),
                published=str(release.get("published_at") or ""),
                assets=assets,
            )
        )
    return tuple(found)


class Catalogue:
    """What has been released, remembered for a while.

    Holds the listing rather than being a function, because the point is that
    the answer is reused: flashing three hubs, or a `list` followed by a
    `flash`, should be one request to GitHub rather than several.
    """

    def __init__(self, fetcher: Fetcher, *, lifetime: float = INDEX_LIFETIME) -> None:
        """Create a catalogue.

        Args:
            fetcher: How to reach the listing.
            lifetime: How long an answer is reused for, in seconds.
        """
        self._fetcher = fetcher
        self._lifetime = lifetime
        self._releases: tuple[Release, ...] = ()
        self._read_at = 0.0

    async def releases(self, *, refresh: bool = False) -> tuple[Release, ...]:
        """Return what has been released, newest first.

        Args:
            refresh: Ask again even if the last answer is still fresh.

        Returns:
            The releases.

        Raises:
            FirmwareUnavailableError: If the listing could not be fetched and
                there is no remembered one to fall back on. Callers that can
                answer from the cache should catch this rather than fail: not
                being able to check for new firmware is not the same as not
                being able to install any.
        """
        fresh = time.monotonic() - self._read_at < self._lifetime
        if self._releases and fresh and not refresh:
            return self._releases

        try:
            body = await self._fetcher.fetch(RELEASES_URL)
        except OSError as exc:
            if self._releases:
                # A stale listing is a better answer than none, and saying so
                # is better than pretending it is current.
                logger.info("using the last known release list: %s", exc)
                return self._releases
            raise FirmwareUnavailableError(str(exc)) from exc

        try:
            document: Any = json.loads(body)
        except json.JSONDecodeError as exc:
            raise FirmwareBadArchiveError(
                f"the release listing is not JSON: {exc}"
            ) from exc

        self._releases = _newest_first(_releases_from(document))
        self._read_at = time.monotonic()
        logger.info("found %d released firmware versions", len(self._releases))
        return self._releases
