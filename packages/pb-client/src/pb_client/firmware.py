# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What firmware exists, and what happens while one is being installed.

Two kinds of thing arrive while a firmware operation runs -- how far it has
got, and what it is doing between the bars -- and they are parsed here into one
union, for the same reason a hub's output and status share a stream: a caller
drawing a screen needs them in the order they happened.

The narration matters more here than it usually would. Most of the time an
install takes is not spent moving bytes: it is spent waiting for a hub to
reboot, or for somebody to hold a button down. A bar alone would leave them
watching nothing at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from anyio.streams.memory import MemoryObjectReceiveStream

from pb_core.protocol import FIRMWARE_PROGRESS, FIRMWARE_STEP

FETCHED = "firmware.fetched"
"""The name the outcome is put on the event stream under.

Not a notification the service ever sends: it is synthesised here when the
request returns, so that the result arrives after the progress it belongs with
rather than having to be collected from somewhere else. Kept next to the real
names so that the parsing below has one place to look.
"""

__all__ = [
    "FETCHED",
    "CachedFirmware",
    "Fetched",
    "FirmwareCatalogue",
    "FirmwareEvent",
    "FirmwareRelease",
    "Progress",
    "Removed",
    "Step",
    "events",
]


@dataclass(frozen=True, slots=True)
class Progress:
    """How far the current phase has got."""

    phase: str
    """What is happening: ``fetching``, ``erasing`` or ``installing``.

    Three phases rather than one because they are three visibly different
    waits. A bar that restarts is less confusing than one that sits at a third
    for a minute.
    """

    done: int
    """Bytes so far."""

    total: int
    """Bytes expected, or zero when the far end did not say.

    Worth checking rather than dividing by: a server that sends no
    content-length is ordinary, not exceptional.
    """


@dataclass(frozen=True, slots=True)
class Step:
    """What the operation is doing, in words.

    This is what stands in for the video a browser can play while somebody
    holds a button down.
    """

    step: str
    """A short name for what is happening."""

    detail: str = ""
    """Anything worth adding, such as which hub was found."""


@dataclass(frozen=True, slots=True)
class FirmwareRelease:
    """One published set of firmware."""

    version: str
    """The version as a user would type it, such as ``4.1.0b3``."""

    prerelease: bool
    """Whether it is a beta.

    Never installed unless asked for, since getting back off one means
    flashing again.
    """

    published: str = ""
    """When it was published."""

    hubs: Sequence[int] = ()
    """Which hub types it has firmware for.

    Older releases have fewer: a hub that did not exist yet is simply absent
    rather than an error.
    """

    cached: Sequence[int] = ()
    """Which of those are already on this computer."""

    def is_cached_for(self, hub_type: int) -> bool:
        """Whether this release is already downloaded for a hub.

        Args:
            hub_type: The hub's ``device-id``.

        Returns:
            Whether it could be installed with no network.
        """
        return hub_type in self.cached

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> FirmwareRelease:
        """Read one release as the service described it.

        Args:
            payload: One entry of ``firmware.list``'s ``releases``.

        Returns:
            The release.
        """
        return cls(
            version=str(payload.get("version", "")),
            prerelease=bool(payload.get("prerelease")),
            published=str(payload.get("published", "")),
            hubs=tuple(_ints(payload.get("hubs"))),
            cached=tuple(_ints(payload.get("cached"))),
        )


@dataclass(frozen=True, slots=True)
class CachedFirmware:
    """One firmware archive on this computer."""

    hub_type: int
    version: str
    size: int
    path: str

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> CachedFirmware:
        """Read one archive as the service described it.

        Args:
            payload: One entry of ``firmware.list``'s ``cached``.

        Returns:
            The archive.
        """
        return cls(
            hub_type=int(payload.get("hub_type", 0)),
            version=str(payload.get("version", "")),
            size=int(payload.get("size", 0)),
            path=str(payload.get("path", "")),
        )


@dataclass(frozen=True, slots=True)
class FirmwareCatalogue:
    """What can be installed, and what is already here."""

    releases: Sequence[FirmwareRelease] = ()
    """What has been published, newest first."""

    cached: Sequence[CachedFirmware] = ()
    """What is on this computer, whether or not it was ever published."""

    cache_dir: str = ""
    """Where the archives are kept.

    Carried here rather than fetched separately, since anything asking what is
    available also wants to say where it would go.
    """

    reached_index: bool = True
    """Whether the published list could be reached.

    False means everything above came from the cache. Not an error -- the
    question still has a useful answer -- but a caller should say so rather
    than imply the list is current.
    """

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> FirmwareCatalogue:
        """Read what ``firmware.list`` returned.

        Args:
            payload: The result.

        Returns:
            The catalogue.
        """
        return cls(
            releases=tuple(
                FirmwareRelease.from_wire(entry)
                for entry in _mappings(payload.get("releases"))
            ),
            cached=tuple(
                CachedFirmware.from_wire(entry)
                for entry in _mappings(payload.get("cached"))
            ),
            cache_dir=str(payload.get("cache_dir", "")),
            reached_index=bool(payload.get("reached_index", True)),
        )


@dataclass(frozen=True, slots=True)
class Fetched:
    """The outcome of putting an archive in the cache."""

    version: str
    """Which version it turned out to be."""

    size: int
    """How big the archive is."""

    cached: bool
    """Whether it was already here, so nothing was downloaded."""

    path: str = ""
    """Where it is now."""

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> Fetched:
        """Read what ``firmware.fetch`` returned.

        Args:
            payload: The result.

        Returns:
            The outcome.
        """
        return cls(
            version=str(payload.get("version", "")),
            size=int(payload.get("size", 0)),
            cached=bool(payload.get("cached")),
            path=str(payload.get("path", "")),
        )


FirmwareEvent = Progress | Step | Fetched
"""Anything that can arrive while a firmware operation runs."""


@dataclass(frozen=True, slots=True)
class Removed:
    """What a clean took away."""

    removed: Sequence[CachedFirmware] = field(
        default_factory=tuple[CachedFirmware, ...]
    )
    cache_dir: str = ""

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> Removed:
        """Read what ``firmware.clean`` returned.

        Args:
            payload: The result.

        Returns:
            What was removed.
        """
        return cls(
            removed=tuple(
                CachedFirmware.from_wire(entry)
                for entry in _mappings(payload.get("removed"))
            ),
            cache_dir=str(payload.get("cache_dir", "")),
        )


def _ints(value: Any) -> list[int]:
    """Read a list of numbers out of a payload without trusting it to be one."""
    if not isinstance(value, list):
        return []
    return [
        item for item in value if isinstance(item, int) and not isinstance(item, bool)
    ]


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    """Read a list of objects out of a payload without trusting it to be one."""
    if not isinstance(value, list):
        return []
    items: list[Mapping[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            items.append(item)
    return items


def _parse(method: str, params: Mapping[str, Any]) -> FirmwareEvent | None:
    """Turn one notification into an event.

    Args:
        method: Which notification arrived.
        params: Its parameters.

    Returns:
        The event, or ``None`` for a notification this build does not know
        about -- a service can be newer than its client, and an unknown event
        is not an error, it is just not for us.
    """
    if method == FIRMWARE_PROGRESS:
        return Progress(
            phase=str(params.get("phase", "")),
            done=int(params.get("done", 0)),
            total=int(params.get("total", 0)),
        )
    if method == FIRMWARE_STEP:
        return Step(
            step=str(params.get("step", "")), detail=str(params.get("detail", ""))
        )
    if method == FETCHED:
        return Fetched.from_wire(params)
    return None


async def events(
    payloads: MemoryObjectReceiveStream[tuple[str, dict[str, Any]]],
) -> AsyncIterator[FirmwareEvent]:
    """Turn the notifications of a firmware operation into events.

    Args:
        payloads: The raw notifications, in the order they arrived.

    Yields:
        Each event that this build understands.
    """
    async with payloads:
        async for method, params in payloads:
            event = _parse(method, params)
            if event is not None:
                yield event
