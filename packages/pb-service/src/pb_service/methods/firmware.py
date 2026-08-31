# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Finding and fetching firmware, over the wire.

Only the half that does not touch a hub: what exists, getting a copy, and
throwing copies away. Installing one onto a hub follows, and needs the
bootloaders.

``firmware.fetch`` is long-running and reports progress as it goes, which is
the same shape ``hub.scan`` already uses -- one request held open for the whole
operation, notifications interleaved with it, and the finished answer returned
at the end so a client that ignores the notifications loses nothing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_core.protocol import FIRMWARE_PROGRESS
from pb_service.firmware import errors as firmware_errors
from pb_service.firmware.cache import CachedImage
from pb_service.firmware.catalogue import ASSET_NAMES, Release, choose
from pb_service.firmware.library import Library
from pb_service.methods.registry import method

if TYPE_CHECKING:
    from pb_service.session import Session

logger = logging.getLogger(__name__)

__all__ = ["firmware_clean", "firmware_fetch", "firmware_list"]

_WIRE_ERRORS = {
    firmware_errors.FirmwareNotFoundError: ErrorCode.FIRMWARE_NOT_FOUND,
    firmware_errors.FirmwareUnavailableError: ErrorCode.FIRMWARE_UNAVAILABLE,
    firmware_errors.FirmwareBadArchiveError: ErrorCode.FIRMWARE_BAD_ARCHIVE,
    firmware_errors.FirmwareIncompatibleError: ErrorCode.FIRMWARE_INCOMPATIBLE,
    firmware_errors.FirmwareHubNameTooLongError: ErrorCode.FIRMWARE_HUB_NAME_TOO_LONG,
}
"""Which wire error each failure becomes.

Translated in one place, as hub failures already are, so that the modules
below know nothing about JSON-RPC and can be tested without it.
"""


def as_wire_error(exc: firmware_errors.FirmwareError) -> JsonRpcError:
    """Describe a firmware failure as the error a client should see.

    Args:
        exc: What went wrong.

    Returns:
        The equivalent wire error. The message is kept: it names the version
        or the file, which is the specific part, and the advice belongs to
        whoever is talking to the user.
    """
    for kind, code in _WIRE_ERRORS.items():
        if isinstance(exc, kind):
            return JsonRpcError(code, str(exc))
    return JsonRpcError(ErrorCode.INTERNAL_ERROR, str(exc))


def _hub_type_from(params: dict[str, Any]) -> int:
    """Read the hub type a request names.

    Args:
        params: The request's parameters.

    Returns:
        The ``device-id``.

    Raises:
        JsonRpcError: If it is missing or is not a hub with firmware.
    """
    value = params.get("hub_type")
    if not isinstance(value, int) or isinstance(value, bool):
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, "hub_type must be a number")
    if value not in ASSET_NAMES:
        raise JsonRpcError(
            ErrorCode.FIRMWARE_NOT_FOUND,
            f"there is no Pybricks firmware for hub type {value:#04x}",
        )
    return value


def _release_json(
    release: Release, cached: frozenset[tuple[int, str]]
) -> dict[str, Any]:
    """Describe one release for a client.

    Args:
        release: The release.
        cached: Which hub type and version pairs are already on this computer.

    Returns:
        Its wire form. ``hubs`` says which hub types the release has firmware
        for, and ``cached`` which of those are here already, so that a client
        can mark them without asking again per hub.
    """
    hubs = sorted(hub for hub, name in ASSET_NAMES.items() if name in release.assets)
    return {
        "version": release.version,
        "tag": release.tag,
        "prerelease": release.prerelease,
        "published": release.published,
        "hubs": hubs,
        "cached": sorted(hub for hub in hubs if (hub, release.version) in cached),
    }


def _from_cache(library: Library, hub_type: int, request: str | None) -> str:
    """Answer a version request from what is on disk.

    The fallback for a machine with no network, where the published list
    cannot be consulted but the answer may well already be here.

    Args:
        library: What this service knows about firmware.
        hub_type: Which hub.
        request: What was asked for, or ``None`` for the newest here.

    Returns:
        The version to use.

    Raises:
        FirmwareUnavailableError: If nothing here answers the request, which
            is a different thing from the version not existing -- the remedy
            is a network or a copied file rather than a different version.
    """
    here = tuple(
        Release(
            version=image.version,
            tag=f"v{image.version}",
            prerelease=False,
            published="",
        )
        for image in library.cache.stored()
        if image.hub_type == hub_type
    )
    try:
        # Nothing here counts as a prerelease, and there is no flag: whether a
        # version is one was a property of the listing, which is precisely
        # what is missing. Somebody who deliberately fetched a beta should be
        # able to install it afterwards without a network to confirm it.
        return choose(here, request, allow_prerelease=True).version
    except firmware_errors.FirmwareNotFoundError as exc:
        raise firmware_errors.FirmwareUnavailableError(
            f"{exc}, and the published list could not be reached"
        ) from exc


def _cached_json(image: CachedImage) -> dict[str, Any]:
    """Describe one archive on disk for a client."""
    return {
        "hub_type": image.hub_type,
        "version": image.version,
        "size": image.size,
        "path": str(image.path),
    }


@method(
    "firmware.list",
    "Report the firmware that can be installed, and which of it is already on "
    "this computer.",
    params={
        "hub_type": "Only report firmware for this hub type.",
        "cached_only": "Do not go to the network; report only what is here.",
        "refresh": "Ask again even if the last listing is still fresh.",
    },
    returns=(
        "The releases, what is cached, where the cache is, and whether the "
        "published list could be reached."
    ),
)
async def firmware_list(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """List released and cached firmware."""
    session.service.mark_activity()
    library = session.service.firmware
    wanted = params.get("hub_type")
    hub_type = _hub_type_from(params) if wanted is not None else None

    stored = [
        image
        for image in library.cache.stored()
        if hub_type is None or image.hub_type == hub_type
    ]
    cached = frozenset((image.hub_type, image.version) for image in stored)

    releases: list[Release] = []
    reached = True
    if not params.get("cached_only"):
        try:
            releases = list(
                await library.catalogue.releases(refresh=bool(params.get("refresh")))
            )
        except firmware_errors.FirmwareUnavailableError as exc:
            # Not being able to check for new firmware is not the same as not
            # being able to install any: the cache still answers the question.
            logger.info("could not reach the release list: %s", exc)
            reached = False
        except firmware_errors.FirmwareError as exc:
            raise as_wire_error(exc) from exc

    if hub_type is not None:
        name = ASSET_NAMES[hub_type]
        releases = [r for r in releases if name in r.assets]

    return {
        "releases": [_release_json(release, cached) for release in releases],
        "cached": [_cached_json(image) for image in stored],
        "cache_dir": str(library.cache.directory),
        "reached_index": reached,
    }


@method(
    "firmware.fetch",
    "Download a firmware archive into this computer's cache.",
    params={
        "hub_type": "Which hub the firmware is for.",
        "version": (
            "Which version, exactly or in part -- 4.1 means the newest of "
            "those. Left out means the newest there is."
        ),
        "allow_prerelease": "Consider prereleases when resolving the version.",
    },
    returns="The version fetched, its size, and whether it was already here.",
)
async def firmware_fetch(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Put a firmware archive in the cache, downloading it if need be."""
    session.service.mark_activity()
    library = session.service.firmware
    hub_type = _hub_type_from(params)
    request = params.get("version")
    if request is not None and not isinstance(request, str):
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, "version must be text")

    allow_prerelease = bool(params.get("allow_prerelease"))

    # An exact version that is already here needs no catalogue and no network.
    # That is not only a saving: it is what makes the cache work at all on a
    # machine with no network, where resolving the version would otherwise
    # fail before anybody discovered the file was sitting right there.
    if request is not None and library.cache.has(hub_type, request):
        return {
            "version": request,
            "size": library.cache.path_for(hub_type, request).stat().st_size,
            "cached": True,
            "path": str(library.cache.path_for(hub_type, request)),
        }

    try:
        try:
            releases = await library.catalogue.releases()
        except firmware_errors.FirmwareUnavailableError:
            # No catalogue. What is on disk is still a set of versions, so a
            # request may well be answerable from it -- "4.0" offline should
            # find the 4.0.1 that was fetched last week rather than give up.
            release = _from_cache(library, hub_type, request)
            return {
                "version": release,
                "size": library.cache.path_for(hub_type, release).stat().st_size,
                "cached": True,
                "path": str(library.cache.path_for(hub_type, release)),
            }

        release = choose(releases, request, allow_prerelease=allow_prerelease)
        url = release.url_for(hub_type)
        already = library.cache.has(hub_type, release.version)

        def report(done: int, total: int) -> None:
            if session.peer is not None:
                session.peer.notify(
                    FIRMWARE_PROGRESS,
                    {"phase": "fetching", "done": done, "total": total},
                )

        body = await library.cache.fetch(
            hub_type, release.version, url, on_progress=report
        )
    except firmware_errors.FirmwareError as exc:
        raise as_wire_error(exc) from exc

    return {
        "version": release.version,
        "size": len(body),
        "cached": already,
        "path": str(library.cache.path_for(hub_type, release.version)),
    }


@method(
    "firmware.clean",
    "Remove downloaded firmware from this computer's cache.",
    params={
        "hub_type": "Only remove firmware for this hub type.",
        "version": "Only remove this version.",
        "all": "Remove everything, rather than one version.",
    },
    returns="What was removed.",
)
async def firmware_clean(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Delete cached archives."""
    session.service.mark_activity()
    library = session.service.firmware
    wanted = params.get("hub_type")
    hub_type = _hub_type_from(params) if wanted is not None else None
    version = params.get("version")
    if version is not None and not isinstance(version, str):
        raise JsonRpcError(ErrorCode.INVALID_PARAMS, "version must be text")

    if version is None and not params.get("all"):
        # Refused rather than guessed at: the two readings of an empty request
        # are "remove nothing" and "remove everything", and one of them is not
        # recoverable.
        raise JsonRpcError(
            ErrorCode.INVALID_PARAMS, "say which version to remove, or ask for all"
        )

    removed: list[dict[str, Any]] = []
    for image in list(library.cache.stored()):
        if hub_type is not None and image.hub_type != hub_type:
            continue
        if version is not None and image.version != version:
            continue
        described = _cached_json(image)
        if await library.cache.remove(image):
            removed.append(described)

    return {"removed": removed, "cache_dir": str(library.cache.directory)}
