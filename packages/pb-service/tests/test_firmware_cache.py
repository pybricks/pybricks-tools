# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The directory of downloaded firmware.

The promise being tested is that it is an ordinary directory of ordinary
files: somebody with no network can drop a zip into it and have it found. That
only holds if fetching one and copying one in produce the same directory, so
that is what most of this checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pb_service.firmware.cache import Cache, archive_name
from pb_service.firmware.errors import FirmwareNotFoundError, FirmwareUnavailableError
from pb_service.firmware.fake import FakeFetcher, tiny_firmware

pytestmark = pytest.mark.anyio

CITY_HUB = 0x41
PRIME_HUB = 0x81
URL = "https://example.invalid/pybricks-cityhub-v4.1.0b3.zip"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(FakeFetcher(), directory=tmp_path / "firmware")


def test_an_archive_is_named_the_way_it_was_published() -> None:
    # Worked out rather than remembered, which is what lets a hand-copied file
    # be found: the user knows what it is called and nothing else about it.
    assert archive_name(CITY_HUB, "4.1.0b3") == "pybricks-cityhub-v4.1.0b3.zip"
    assert archive_name(PRIME_HUB, "4.0.1") == "pybricks-primehub-v4.0.1.zip"


def test_a_hub_with_no_firmware_has_no_archive_name() -> None:
    with pytest.raises(FirmwareNotFoundError):
        archive_name(0x84, "4.0.1")


async def test_fetching_stores_it_under_that_name(tmp_path: Path) -> None:
    body = tiny_firmware(CITY_HUB, "4.1.0b3")
    directory = tmp_path / "firmware"
    cache = Cache(FakeFetcher(bodies={URL: body}), directory=directory)

    assert not cache.has(CITY_HUB, "4.1.0b3")
    assert await cache.fetch(CITY_HUB, "4.1.0b3", URL) == body
    assert cache.has(CITY_HUB, "4.1.0b3")
    assert (directory / "pybricks-cityhub-v4.1.0b3.zip").read_bytes() == body


async def test_a_cached_archive_is_not_fetched_again(tmp_path: Path) -> None:
    body = tiny_firmware(CITY_HUB, "4.1.0b3")
    fetcher = FakeFetcher(bodies={URL: body})
    cache = Cache(fetcher, directory=tmp_path / "firmware")

    await cache.fetch(CITY_HUB, "4.1.0b3", URL)
    await cache.fetch(CITY_HUB, "4.1.0b3", URL)
    assert fetcher.count(URL) == 1


async def test_a_file_copied_in_by_hand_is_found(tmp_path: Path) -> None:
    # The whole offline story in one test. Nothing was ever fetched, and the
    # fetcher would fail if it were asked.
    directory = tmp_path / "firmware"
    directory.mkdir(parents=True)
    body = tiny_firmware(CITY_HUB, "4.1.0b3")
    (directory / "pybricks-cityhub-v4.1.0b3.zip").write_bytes(body)

    cache = Cache(FakeFetcher(), directory=directory)
    assert cache.has(CITY_HUB, "4.1.0b3")
    assert await cache.fetch(CITY_HUB, "4.1.0b3", URL) == body
    assert await cache.read(CITY_HUB, "4.1.0b3") == body


async def test_reading_something_that_is_not_here_says_so(cache: Cache) -> None:
    with pytest.raises(FirmwareUnavailableError, match="not in the cache"):
        await cache.read(CITY_HUB, "4.1.0b3")


async def test_a_download_that_fails_is_reported(tmp_path: Path) -> None:
    fetcher = FakeFetcher(failures={URL: "the network went away"})
    cache = Cache(fetcher, directory=tmp_path / "firmware")
    with pytest.raises(FirmwareUnavailableError, match="went away"):
        await cache.fetch(CITY_HUB, "4.1.0b3", URL)
    # And nothing half-written was left behind to be mistaken for an archive.
    assert (
        list((tmp_path / "firmware").glob("*")) == []
        or not (tmp_path / "firmware").exists()
    )


async def test_progress_is_reported_while_it_arrives(tmp_path: Path) -> None:
    body = tiny_firmware(CITY_HUB, "4.1.0b3")
    cache = Cache(FakeFetcher(bodies={URL: body}), directory=tmp_path / "firmware")
    seen: list[tuple[int, int]] = []
    await cache.fetch(
        CITY_HUB, "4.1.0b3", URL, on_progress=lambda a, b: seen.append((a, b))
    )
    assert seen and seen[-1] == (len(body), len(body))


# ------------------------------------------------------------- what is here


async def test_what_is_stored_is_reported(tmp_path: Path) -> None:
    directory = tmp_path / "firmware"
    cache = Cache(
        FakeFetcher(
            bodies={
                "a": tiny_firmware(CITY_HUB, "4.0.1"),
                "b": tiny_firmware(CITY_HUB, "4.1.0b3"),
                "c": tiny_firmware(PRIME_HUB, "4.0.1"),
            }
        ),
        directory=directory,
    )
    await cache.fetch(CITY_HUB, "4.0.1", "a")
    await cache.fetch(CITY_HUB, "4.1.0b3", "b")
    await cache.fetch(PRIME_HUB, "4.0.1", "c")

    stored = sorted((i.hub_type, i.version) for i in cache.stored())
    assert stored == [(CITY_HUB, "4.0.1"), (CITY_HUB, "4.1.0b3"), (PRIME_HUB, "4.0.1")]
    assert all(image.size > 0 for image in cache.stored())


def test_an_empty_or_missing_directory_reports_nothing(cache: Cache) -> None:
    assert list(cache.stored()) == []


def test_somebody_elses_files_are_left_alone(tmp_path: Path) -> None:
    # The directory belongs to the user. Something of theirs being in it is
    # not an error, and is not ours to report or to delete.
    directory = tmp_path / "firmware"
    directory.mkdir(parents=True)
    (directory / "notes.txt").write_text("mine")
    (directory / "pybricks-something-else.txt").write_text("also mine")
    (directory / "pybricks-nosuchhub-v1.zip").write_bytes(b"")
    (directory / "pybricks-cityhub-4.0.1.zip").write_bytes(b"")  # no v

    assert list(Cache(FakeFetcher(), directory=directory).stored()) == []


async def test_removing_one_leaves_the_rest(tmp_path: Path) -> None:
    directory = tmp_path / "firmware"
    cache = Cache(
        FakeFetcher(
            bodies={
                "a": tiny_firmware(CITY_HUB, "4.0.1"),
                "b": tiny_firmware(CITY_HUB, "4.1.0b3"),
            }
        ),
        directory=directory,
    )
    await cache.fetch(CITY_HUB, "4.0.1", "a")
    await cache.fetch(CITY_HUB, "4.1.0b3", "b")

    beta = next(i for i in cache.stored() if i.version == "4.1.0b3")
    assert await cache.remove(beta)
    assert [i.version for i in cache.stored()] == ["4.0.1"]
    # And removing it twice is not an error, just nothing to do.
    assert not await cache.remove(beta)
