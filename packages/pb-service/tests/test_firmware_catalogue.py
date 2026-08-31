# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Which firmware exists, and which of it somebody meant.

The choosing is pure and is where the rules live, so most of this is a table
of requests and what they should resolve to. The listing is a network call and
is exercised through a fake, because a suite that reaches GitHub fails on an
aeroplane and spends the sixty requests an hour an unauthenticated caller gets.
"""

from __future__ import annotations

import pytest

from pb_service.firmware.catalogue import (
    RELEASES_URL,
    Catalogue,
    Release,
    asset_name_for,
    choose,
)
from pb_service.firmware.errors import (
    FirmwareBadArchiveError,
    FirmwareNotFoundError,
    FirmwareUnavailableError,
)
from pb_service.firmware.fake import FakeFetcher, release_listing

pytestmark = pytest.mark.anyio

CITY_HUB = 0x41
PRIME_HUB = 0x81

# What the real listing looked like in August 2026, in the order GitHub gave
# it. Deliberately not sorted, so that anything relying on the order having
# been fixed is exercised.
PUBLISHED = (
    ("4.1.0b3", True),
    ("4.1.0b2", True),
    ("4.0.1", False),
    ("4.0.0", False),
    ("4.0.0b11", True),
    ("3.6.1", False),
)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


def released(*versions: tuple[str, bool]) -> tuple[Release, ...]:
    """Build releases without going anywhere near a network."""
    return tuple(
        Release(
            version=version,
            tag=f"v{version}",
            prerelease=prerelease,
            published="2026-08-27T09:50:30Z",
            assets={"cityhub": f"https://example.invalid/{version}/cityhub.zip"},
        )
        for version, prerelease in versions
    )


# ------------------------------------------------------------------ choosing


@pytest.mark.parametrize(
    ("request_", "allow_prerelease", "expected"),
    [
        # Nothing asked for is the newest stable, which is the whole default
        # behaviour of `pb firmware flash`.
        (None, False, "4.0.1"),
        (None, True, "4.1.0b3"),
        # A partial version is the newest of those.
        ("4.0", False, "4.0.1"),
        ("4", False, "4.0.1"),
        ("4.1", True, "4.1.0b3"),
        ("3", False, "3.6.1"),
        # An exact version is itself.
        ("4.0.0", False, "4.0.0"),
        # And an exact prerelease is itself even without the flag: refusing to
        # install what somebody literally typed would be pedantry.
        ("4.1.0b3", False, "4.1.0b3"),
        ("4.0.0b11", False, "4.0.0b11"),
    ],
)
def test_a_version_request_resolves_to_what_was_meant(
    request_: str | None, allow_prerelease: bool, expected: str
) -> None:
    chosen = choose(released(*PUBLISHED), request_, allow_prerelease=allow_prerelease)
    assert chosen.version == expected


def test_a_partial_version_is_compared_as_a_version_not_as_text() -> None:
    # The reason this is not string matching: "4.1" must not answer "4.10".
    releases = released(("4.10.0", False), ("4.1.0", False))
    assert choose(releases, "4.1").version == "4.1.0"


def test_a_prerelease_is_not_handed_to_somebody_who_did_not_ask() -> None:
    # A hub is a bad place to be given a beta by surprise: getting back off
    # one means flashing again. Asking for 4.1 when every 4.1 is a beta is
    # refused rather than quietly answered with 4.0.1 -- that is not what was
    # asked for -- but the message has to say which of the two situations it
    # is, because "no such version" and "only a beta" read the same from
    # outside and only one of them has a fix.
    with pytest.raises(FirmwareNotFoundError, match="prerelease"):
        choose(released(*PUBLISHED), "4.1", allow_prerelease=False)


def test_only_prereleases_of_a_requested_version_is_told_apart_from_none() -> None:
    with pytest.raises(FirmwareNotFoundError, match="no firmware version matches"):
        choose(released(*PUBLISHED), "9.9", allow_prerelease=True)


def test_asking_for_a_version_that_was_never_published_says_so() -> None:
    with pytest.raises(FirmwareNotFoundError, match=r"9\.9"):
        choose(released(*PUBLISHED), "9.9")


def test_nothing_released_at_all_is_its_own_message() -> None:
    with pytest.raises(FirmwareNotFoundError, match="no released firmware"):
        choose((), None)


def test_only_prereleases_released_suggests_the_flag() -> None:
    # Which is the difference between a dead end and a next step.
    with pytest.raises(FirmwareNotFoundError, match="prerelease"):
        choose(released(("4.1.0b3", True)), None)


def test_a_version_that_is_not_a_version_is_ignored_rather_than_crashed_on() -> None:
    # A tag somebody pushed by hand should not stop the list being usable.
    releases = released(("not-a-version", False), ("4.0.1", False))
    assert choose(releases, None).version == "4.0.1"


# -------------------------------------------------------------- asset naming


@pytest.mark.parametrize(
    ("hub_type", "expected"),
    [(0x40, "movehub"), (0x41, "cityhub"), (0x81, "primehub"), (0xE2, "ev3")],
)
def test_each_hub_type_has_its_own_spelling(hub_type: int, expected: str) -> None:
    assert asset_name_for(hub_type) == expected


def test_a_hub_with_no_pybricks_firmware_is_refused() -> None:
    with pytest.raises(FirmwareNotFoundError, match="no Pybricks firmware"):
        asset_name_for(0x84)


def test_a_release_that_predates_a_hub_says_so_rather_than_guessing() -> None:
    old = Release("3.0.0", "v3.0.0", False, "", {"cityhub": "https://example.invalid"})
    with pytest.raises(FirmwareNotFoundError, match="nothing for a primehub"):
        old.url_for(PRIME_HUB)


# ------------------------------------------------------------- the listing


async def test_the_listing_is_read_into_releases() -> None:
    fetcher = FakeFetcher(bodies={RELEASES_URL: release_listing(*PUBLISHED)})
    releases = await Catalogue(fetcher).releases()
    assert [r.version for r in releases] == [
        "4.1.0b3",
        "4.1.0b2",
        "4.0.1",
        "4.0.0",
        "4.0.0b11",
        "3.6.1",
    ]
    assert releases[0].url_for(CITY_HUB).endswith("pybricks-cityhub-v4.1.0b3.zip")


async def test_the_listing_is_asked_for_once_and_then_reused() -> None:
    # Sixty requests an hour, and flashing several hubs in a row should not
    # spend one each.
    fetcher = FakeFetcher(bodies={RELEASES_URL: release_listing(*PUBLISHED)})
    catalogue = Catalogue(fetcher)
    for _ in range(5):
        await catalogue.releases()
    assert fetcher.count(RELEASES_URL) == 1


async def test_refreshing_asks_again() -> None:
    fetcher = FakeFetcher(bodies={RELEASES_URL: release_listing(*PUBLISHED)})
    catalogue = Catalogue(fetcher)
    await catalogue.releases()
    await catalogue.releases(refresh=True)
    assert fetcher.count(RELEASES_URL) == 2


async def test_a_stale_listing_is_better_than_none() -> None:
    # Losing the network after one successful look should not stop the tool
    # answering; it just cannot promise the answer is current.
    fetcher = FakeFetcher(bodies={RELEASES_URL: release_listing(*PUBLISHED)})
    catalogue = Catalogue(fetcher, lifetime=0)
    first = await catalogue.releases()
    fetcher.failures[RELEASES_URL] = "the network went away"
    assert await catalogue.releases() == first


async def test_no_listing_and_nothing_remembered_is_reported() -> None:
    fetcher = FakeFetcher(failures={RELEASES_URL: "the network went away"})
    with pytest.raises(FirmwareUnavailableError, match="went away"):
        await Catalogue(fetcher).releases()


async def test_a_listing_that_is_not_json_is_refused() -> None:
    fetcher = FakeFetcher(bodies={RELEASES_URL: b"<html>rate limited</html>"})
    with pytest.raises(FirmwareBadArchiveError, match="not JSON"):
        await Catalogue(fetcher).releases()


async def test_a_listing_of_the_wrong_shape_is_refused() -> None:
    fetcher = FakeFetcher(bodies={RELEASES_URL: b'{"message": "API rate limit"}'})
    with pytest.raises(FirmwareBadArchiveError, match="not a list"):
        await Catalogue(fetcher).releases()


async def test_entries_that_make_no_sense_are_skipped_not_fatal() -> None:
    # A service newer than this build, or a draft release, should cost the
    # user the entry rather than the whole list.
    body = (
        b'[{"tag_name": "v4.0.1", "assets": []},'
        b' {"no_tag": true},'
        b' {"tag_name": "v9.9.9", "draft": true, "assets": []}]'
    )
    releases = await Catalogue(FakeFetcher(bodies={RELEASES_URL: body})).releases()
    assert [r.version for r in releases] == ["4.0.1"]
