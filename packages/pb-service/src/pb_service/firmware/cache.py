# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Where downloaded firmware is kept.

A plain directory of plain files, in the place each operating system keeps
caches, and that plainness is the feature. Somebody with no network can put a
zip they downloaded elsewhere into it by hand and have it found, which is the
whole of the offline story; a cache with its own naming scheme or its own
container format would make that impossible and would make ``pb firmware dir``
useless to a human.

So an archive is stored under the name it was published as --
``pybricks-cityhub-v4.1.0b3.zip`` -- unrepacked and unrenamed. Fetching one
and copying one in have to produce the same directory, or the promise above is
not true.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import anyio

from pb_core import paths
from pb_service.firmware.catalogue import ASSET_NAMES, asset_name_for
from pb_service.firmware.errors import FirmwareNotFoundError, FirmwareUnavailableError
from pb_service.firmware.fetch import Fetcher, Progress

logger = logging.getLogger(__name__)

__all__ = ["Cache", "CachedImage", "archive_name"]


def archive_name(hub_type: int, version: str) -> str:
    """Return what a release's archive for a hub is called.

    The published name, exactly. Working it out rather than remembering it is
    what lets a hand-copied file be found: the user did not record where it
    came from, only what it is called.

    Args:
        hub_type: The ``device-id`` of the hub.
        version: The firmware version, without a leading ``v``.

    Returns:
        The file name.

    Raises:
        FirmwareNotFoundError: If there is no Pybricks firmware for that hub.
    """
    return f"pybricks-{asset_name_for(hub_type)}-v{version}.zip"


@dataclass(frozen=True, slots=True)
class CachedImage:
    """One archive on disk."""

    hub_type: int
    """Which hub it is for."""

    version: str
    """Which firmware version it is."""

    path: Path
    """Where it is."""

    size: int
    """How big it is, in bytes."""


class Cache:
    """The directory of downloaded firmware."""

    def __init__(self, fetcher: Fetcher, *, directory: Path | None = None) -> None:
        """Create a cache.

        Args:
            fetcher: How to get anything that is not here yet.
            directory: Where to keep things. Defaults to the platform's cache
                directory, which ``PB_CACHE_DIR`` can move.
        """
        self._fetcher = fetcher
        self._directory = directory if directory is not None else paths.cache_dir()

    @property
    def directory(self) -> Path:
        """Where the archives are kept."""
        return self._directory

    def path_for(self, hub_type: int, version: str) -> Path:
        """Return where an archive would be, whether or not it is there.

        Args:
            hub_type: The ``device-id`` of the hub.
            version: The firmware version.

        Returns:
            The path.
        """
        return self._directory / archive_name(hub_type, version)

    def has(self, hub_type: int, version: str) -> bool:
        """Whether an archive is already here.

        Args:
            hub_type: The ``device-id`` of the hub.
            version: The firmware version.

        Returns:
            Whether it is on disk.
        """
        try:
            return self.path_for(hub_type, version).is_file()
        except FirmwareNotFoundError:
            return False

    def stored(self) -> Iterator[CachedImage]:
        """Report every archive here that we can make sense of.

        A file whose name does not parse is left alone rather than reported or
        deleted: the directory belongs to the user, and something else of
        theirs being in it is not an error.

        Yields:
            Each archive, in name order.
        """
        if not self._directory.is_dir():
            return
        by_asset = {name: hub for hub, name in ASSET_NAMES.items()}
        for path in sorted(self._directory.glob("pybricks-*.zip")):
            parts = path.stem.split("-", 2)
            if len(parts) != 3:
                continue
            _, asset, tagged = parts
            hub_type = by_asset.get(asset)
            if hub_type is None or not tagged.startswith("v"):
                continue
            yield CachedImage(
                hub_type=hub_type,
                version=tagged[1:],
                path=path,
                size=path.stat().st_size,
            )

    async def read(self, hub_type: int, version: str) -> bytes:
        """Return an archive that is already here.

        Args:
            hub_type: The ``device-id`` of the hub.
            version: The firmware version.

        Returns:
            The archive.

        Raises:
            FirmwareUnavailableError: If it is not here.
        """
        path = self.path_for(hub_type, version)
        try:
            return await anyio.Path(path).read_bytes()
        except OSError as exc:
            raise FirmwareUnavailableError(f"{path.name} is not in the cache") from exc

    async def fetch(
        self,
        hub_type: int,
        version: str,
        url: str,
        *,
        on_progress: Progress | None = None,
    ) -> bytes:
        """Return an archive, downloading it if it is not already here.

        Args:
            hub_type: The ``device-id`` of the hub.
            version: The firmware version.
            url: Where to get it from if it is missing.
            on_progress: Called as the bytes arrive.

        Returns:
            The archive.

        Raises:
            FirmwareUnavailableError: If it is missing and cannot be fetched.
        """
        path = self.path_for(hub_type, version)
        if path.is_file():
            logger.debug("using the cached %s", path.name)
            return await anyio.Path(path).read_bytes()

        logger.info("fetching %s", url)
        try:
            body = await self._fetcher.fetch(url, on_progress=on_progress)
        except OSError as exc:
            raise FirmwareUnavailableError(str(exc)) from exc

        await self._store(path, body)
        return body

    async def _store(self, path: Path, body: bytes) -> None:
        """Write an archive into the cache, or explain why it stayed out.

        Written to a neighbouring name and moved into place, so that an
        interrupted download cannot leave something that looks like a usable
        archive. A cache that cannot be written to is not a reason to fail:
        the bytes are in hand either way, and the only cost is fetching them
        again next time.

        Args:
            path: Where it belongs.
            body: What to write.
        """
        temporary = path.with_name(f"{path.name}.part")
        try:
            await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
            await anyio.Path(temporary).write_bytes(body)
            await anyio.Path(temporary).replace(path)
            logger.info("cached %s (%d bytes)", path.name, len(body))
        except OSError:
            logger.warning("could not cache %s", path.name, exc_info=True)
            with anyio.CancelScope(shield=True):
                await anyio.Path(temporary).unlink(missing_ok=True)

    async def remove(self, image: CachedImage) -> bool:
        """Delete one archive.

        Args:
            image: Which one, as reported by :meth:`stored`.

        Returns:
            Whether it was there to delete.
        """
        try:
            await anyio.Path(image.path).unlink()
        except FileNotFoundError:
            return False
        logger.info("removed %s", image.path.name)
        return True
