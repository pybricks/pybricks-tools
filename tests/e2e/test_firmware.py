# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Finding and fetching firmware, end to end.

The real client talking to the real service over a real socket or named pipe,
with only the web faked. That is the seam worth faking: everything below it is
the code that will run, and above it is a network that would make the suite
fail on an aeroplane and spend the sixty requests an hour GitHub allows.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import pytest

import pb_client

# Imported to register the RPC methods.
import pb_service.methods.firmware  # pyright: ignore[reportUnusedImport]
import pb_service.methods.hub  # pyright: ignore[reportUnusedImport]
import pb_service.methods.service  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pb_cli import errors
from pb_cli.commands import firmware as firmware_commands
from pb_core.ipc import Endpoint
from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_service import service as service_module
from pb_service.firmware.cache import Cache
from pb_service.firmware.catalogue import RELEASES_URL, Catalogue
from pb_service.firmware.fake import FakeFetcher, release_listing, tiny_firmware
from pb_service.firmware.library import Library
from pb_service.hub.fake import FakeHub, FakeHubTransport

pytestmark = pytest.mark.anyio

CITY_HUB = 0x41
PRIME_HUB = 0x81
PUBLISHED = (("4.1.0b3", True), ("4.0.1", False), ("4.0.0", False))


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def endpoint(tmp_path: Path) -> Endpoint:
    unique = uuid.uuid4().hex[:12]
    if sys.platform == "win32":
        return Endpoint("pipe", f"pb-fw-{unique}")
    return Endpoint("unix", str(tmp_path / f"{unique}.sock"))


@pytest.fixture
def web(tmp_path: Path) -> FakeFetcher:
    """A web holding three releases and the archives they point at."""
    fetcher = FakeFetcher(bodies={RELEASES_URL: release_listing(*PUBLISHED)})
    for version, _ in PUBLISHED:
        for hub_type in (CITY_HUB, PRIME_HUB):
            name = "cityhub" if hub_type == CITY_HUB else "primehub"
            url = (
                "https://github.com/pybricks/pybricks-micropython/releases"
                f"/download/v{version}/pybricks-{name}-v{version}.zip"
            )
            fetcher.bodies[url] = tiny_firmware(hub_type, version)
    return fetcher


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def _private_runtime(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cache_dir: Path
) -> Iterator[None]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("PB_STATE_DIR", str(tmp_path / "state"))
    # Without this the suite would fill the firmware cache of whoever ran it.
    monkeypatch.setenv("PB_CACHE_DIR", str(cache_dir))
    monkeypatch.delenv("PB_ENDPOINT", raising=False)
    yield


@pytest.fixture
async def service(
    endpoint: Endpoint,
    web: FakeFetcher,
    cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    _private_runtime: None,
) -> AsyncGenerator[None, None]:
    monkeypatch.setenv("PB_ENDPOINT", str(endpoint))
    hub = FakeHub(name="Bumblebee", address="00:11:22:33:44:55")
    async with service_module.run_service(
        endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
    ) as running:
        # The service builds its library lazily from the environment; here it
        # gets one whose web is the fixture above.
        running._firmware = Library(  # pyright: ignore[reportPrivateUsage]
            catalogue=Catalogue(web), cache=Cache(web, directory=cache_dir)
        )
        yield None


async def test_the_service_says_it_can_install_firmware(service: None) -> None:
    # A client older or newer than the service asks by name rather than by
    # version number, so this is what makes the commands available at all.
    async with pb_client.connect(autostart=False) as pb:
        assert "firmware" in pb.capabilities


async def test_listing_reports_what_is_published(service: None) -> None:
    async with pb_client.connect(autostart=False) as pb:
        catalogue = await pb.firmware_catalogue()

    assert [r.version for r in catalogue.releases] == ["4.1.0b3", "4.0.1", "4.0.0"]
    assert catalogue.reached_index
    assert catalogue.cached == ()
    assert catalogue.cache_dir


async def test_fetching_reports_progress_and_then_the_outcome(
    service: None, cache_dir: Path
) -> None:
    seen: list[pb_client.FirmwareEvent] = []
    async with (
        pb_client.connect(autostart=False) as pb,
        pb.fetching_firmware(hub_type=CITY_HUB) as events,
    ):
        async for event in events:
            seen.append(event)

    # The outcome is the last thing on the stream, after the progress it
    # belongs with, rather than something to be collected separately.
    outcome = seen[-1]
    assert isinstance(outcome, pb_client.Fetched)
    assert outcome.version == "4.0.1"
    assert not outcome.cached
    assert outcome.size > 0

    assert any(isinstance(event, pb_client.Progress) for event in seen)
    assert (cache_dir / "pybricks-cityhub-v4.0.1.zip").is_file()


async def test_a_prerelease_is_only_fetched_when_asked_for(service: None) -> None:
    async with pb_client.connect(autostart=False) as pb:
        async with pb.fetching_firmware(hub_type=CITY_HUB) as events:
            stable = [e async for e in events][-1]
        async with pb.fetching_firmware(
            hub_type=CITY_HUB, allow_prerelease=True
        ) as events:
            beta = [e async for e in events][-1]

    assert isinstance(stable, pb_client.Fetched)
    assert isinstance(beta, pb_client.Fetched)
    assert stable.version == "4.0.1"
    assert beta.version == "4.1.0b3"


async def test_what_was_fetched_shows_up_as_cached(service: None) -> None:
    async with pb_client.connect(autostart=False) as pb:
        async with pb.fetching_firmware(hub_type=CITY_HUB, version="4.0.0") as events:
            async for _ in events:
                pass

        catalogue = await pb.firmware_catalogue()
        assert [(c.hub_type, c.version) for c in catalogue.cached] == [
            (CITY_HUB, "4.0.0")
        ]
        # And the release it belongs to knows it, so a listing can mark it
        # without asking again per hub.
        release = next(r for r in catalogue.releases if r.version == "4.0.0")
        assert release.is_cached_for(CITY_HUB)
        assert not release.is_cached_for(PRIME_HUB)


async def test_asking_only_for_what_is_here_never_touches_the_network(
    service: None, web: FakeFetcher
) -> None:
    async with pb_client.connect(autostart=False) as pb:
        await pb.firmware_catalogue(cached_only=True)
    assert web.count(RELEASES_URL) == 0


async def test_a_version_that_does_not_exist_is_a_friendly_failure(
    service: None,
) -> None:
    async with pb_client.connect(autostart=False) as pb:
        with pytest.raises(BaseExceptionGroup) as caught:
            async with pb.fetching_firmware(hub_type=CITY_HUB, version="9.9") as events:
                async for _ in events:
                    pass

    errors = [e for e in caught.value.exceptions if isinstance(e, JsonRpcError)]
    assert errors and errors[0].code == ErrorCode.FIRMWARE_NOT_FOUND


async def test_a_hub_with_no_pybricks_firmware_is_refused(service: None) -> None:
    # The Technic Move hub, which has no Pybricks firmware at all.
    async with pb_client.connect(autostart=False) as pb:
        with pytest.raises(BaseExceptionGroup) as caught:
            async with pb.fetching_firmware(hub_type=0x84) as events:
                async for _ in events:
                    pass

    errors = [e for e in caught.value.exceptions if isinstance(e, JsonRpcError)]
    assert errors and errors[0].code == ErrorCode.FIRMWARE_NOT_FOUND


async def test_cleaning_takes_it_away_again(service: None, cache_dir: Path) -> None:
    async with pb_client.connect(autostart=False) as pb:
        for version in ("4.0.0", "4.0.1"):
            async with pb.fetching_firmware(
                hub_type=CITY_HUB, version=version
            ) as events:
                async for _ in events:
                    pass

        removed = await pb.clean_firmware(version="4.0.0")
        assert [c.version for c in removed.removed] == ["4.0.0"]

        left = await pb.firmware_catalogue(cached_only=True)
        assert [c.version for c in left.cached] == ["4.0.1"]

    assert not (cache_dir / "pybricks-cityhub-v4.0.0.zip").exists()


async def test_cleaning_everything_needs_saying_so(service: None) -> None:
    # "Remove nothing" and "remove everything" are the two readings of an
    # empty request, and only one of them can be undone.
    #
    # Raised plainly rather than in a group, unlike the fetch failures above:
    # this call opens no task group of its own, and a group is only ever what
    # a task group put round something.
    async with pb_client.connect(autostart=False) as pb:
        with pytest.raises(JsonRpcError) as caught:
            await pb.clean_firmware()

    assert caught.value.code == ErrorCode.INVALID_PARAMS


async def test_losing_the_web_still_answers_from_the_cache(
    service: None, web: FakeFetcher
) -> None:
    async with pb_client.connect(autostart=False) as pb:
        async with pb.fetching_firmware(hub_type=CITY_HUB, version="4.0.1") as events:
            async for _ in events:
                pass

        # Now the network goes away, and the listing has never been asked for
        # in a way that would have cached it beyond its lifetime.
        web.failures[RELEASES_URL] = "the network went away"
        catalogue = await pb.firmware_catalogue(refresh=True)

    # Still an answer, and honest about what it could not check.
    assert [c.version for c in catalogue.cached] == ["4.0.1"]


# ------------------------------------------------------- through the commands


async def test_listing_prints_versions_for_a_script(
    service: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # Piped, so tab separated and parseable: the version, whether it is a
    # beta, whether it is here, and which hubs it covers.
    assert await firmware_commands.firmware_list() == errors.EXIT_OK
    captured = capsys.readouterr()
    rows = [line.split("\t") for line in captured.out.splitlines()]
    assert [row[0] for row in rows] == ["4.0.1", "4.0.0"]
    assert all(row[1] == "released" for row in rows)
    assert "cityhub" in rows[0][3]


async def test_listing_hides_betas_unless_asked(
    service: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        await firmware_commands.firmware_list(allow_prerelease=True) == errors.EXIT_OK
    )
    versions = [line.split("\t")[0] for line in capsys.readouterr().out.splitlines()]
    assert versions[0] == "4.1.0b3"


async def test_an_unknown_hub_type_lists_the_ones_there_are(
    service: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # A typo should not be answered with an empty table.
    assert (
        await firmware_commands.firmware_list(hub_type="citihub") == errors.EXIT_ERROR
    )
    captured = capsys.readouterr()
    assert "citihub" in captured.err
    assert "cityhub" in captured.err
    assert captured.out == ""


async def test_fetching_says_what_it_got(
    service: None, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await firmware_commands.firmware_fetch(hub_type="cityhub") == errors.EXIT_OK
    captured = capsys.readouterr()
    assert "4.0.1" in captured.err
    # Nothing but data on stdout, and this command has none.
    assert captured.out == ""
    assert (cache_dir / "pybricks-cityhub-v4.0.1.zip").is_file()


async def test_fetching_the_same_thing_twice_says_it_was_already_here(
    service: None, capsys: pytest.CaptureFixture[str]
) -> None:
    await firmware_commands.firmware_fetch(hub_type="cityhub")
    capsys.readouterr()
    assert await firmware_commands.firmware_fetch(hub_type="cityhub") == errors.EXIT_OK
    assert "already here" in capsys.readouterr().err


async def test_a_version_nobody_published_is_a_friendly_line(
    service: None, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = await firmware_commands.firmware_fetch("9.9", hub_type="cityhub")
    captured = capsys.readouterr()
    assert exit_code == errors.EXIT_ERROR
    assert "9.9" in captured.err
    assert "pb firmware list" in captured.err
    assert captured.out == ""


async def test_the_cache_directory_is_printed_on_its_own(
    service: None, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # So that `cd "$(pb firmware dir)"` works: the path is the answer, so it
    # is data, so it goes to stdout with nothing else on it.
    assert await firmware_commands.firmware_dir() == errors.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out.strip() == str(cache_dir)


async def test_cleaning_needs_to_be_told_what_to_remove(
    service: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await firmware_commands.firmware_clean() == errors.EXIT_ERROR
    assert "--all" in capsys.readouterr().err


async def test_cleaning_one_version_leaves_the_others(
    service: None, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await firmware_commands.firmware_fetch("4.0.0", hub_type="cityhub")
    await firmware_commands.firmware_fetch("4.0.1", hub_type="cityhub")
    capsys.readouterr()

    assert await firmware_commands.firmware_clean("4.0.0") == errors.EXIT_OK
    assert "Removed 1" in capsys.readouterr().err
    assert not (cache_dir / "pybricks-cityhub-v4.0.0.zip").exists()
    assert (cache_dir / "pybricks-cityhub-v4.0.1.zip").is_file()


async def test_cleaning_everything_empties_it(
    service: None, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await firmware_commands.firmware_fetch("4.0.0", hub_type="cityhub")
    await firmware_commands.firmware_fetch("4.0.0", hub_type="primehub")
    capsys.readouterr()

    assert await firmware_commands.firmware_clean(all=True) == errors.EXIT_OK
    assert not (cache_dir / "pybricks-cityhub-v4.0.0.zip").exists()
    assert not (cache_dir / "pybricks-primehub-v4.0.0.zip").exists()


async def test_cleaning_refuses_a_version_and_all_together(
    service: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # The two say different things, so obeying either would be a guess.
    assert (
        await firmware_commands.firmware_clean("4.0.0", all=True) == errors.EXIT_ERROR
    )
    assert capsys.readouterr().err
