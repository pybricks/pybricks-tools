# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Firmware: what there is, and getting a copy of it.

Two halves, and the verbs are chosen so that nobody has to read the help to
tell them apart. ``fetch`` and ``clean`` act on **this computer**; ``flash``
and ``restore``, when they arrive, act on **the hub**.

That is why neither half says ``install``. It has two readings here and both
are plausible -- get an image from the web, or put firmware on a hub -- and
``download`` is worse still, since ``pb download`` already sends a program *to*
a hub and would then mean the opposite direction in the same command line.
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

import cyclopts

import pb_client
from pb_cli import errors, ui
from pb_cli.app import Verbose, configure_logging, firmware_app
from pb_cli.errors import EXIT_OK

logger = logging.getLogger(__name__)

__all__ = ["firmware_clean", "firmware_dir", "firmware_fetch", "firmware_list"]

HUB_TYPES: Final[dict[str, int]] = {
    "movehub": 0x40,
    "cityhub": 0x41,
    "technichub": 0x80,
    "primehub": 0x81,
    "essentialhub": 0x83,
    "nxt": 0xE1,
    "ev3": 0xE2,
}
"""What a user types for each hub, and the id the wire uses.

Spelled the way the release assets are, because that is what somebody sees if
they ever look in the cache directory or download a file by hand, and having
two spellings for the same thing is how people end up with the wrong one.
"""

HUB_LABELS: Final[dict[int, str]] = {
    0x40: "Move hub",
    0x41: "City hub",
    0x80: "Technic hub",
    0x81: "Prime or Inventor hub",
    0x83: "Essential hub",
    0xE1: "NXT",
    0xE2: "EV3",
}
"""What to call each hub in a table, which is not what it is typed as.

"Prime or Inventor hub" because one image serves both, and telling somebody
with an Inventor hub that there is only firmware for a Prime would be wrong.
"""

AllowPrerelease = Annotated[
    bool,
    cyclopts.Parameter(
        name=["--allow-prerelease"],
        help="Consider beta versions when working out which firmware to use.",
        show_default=False,
    ),
]
"""Whether a version request may resolve to a beta.

Off by default, because a hub is a bad place to be handed one by surprise:
getting back off a beta means flashing again. Naming a beta exactly still
works without it -- refusing to fetch what somebody literally typed would be
pedantry rather than safety.
"""


def _hub_type(name: str | None) -> int | None:
    """Turn what a user typed into a hub type.

    Args:
        name: What they typed, or ``None`` for every hub.

    Returns:
        The id, or ``None`` for every hub.

    Raises:
        KeyError: If it is not a hub anyone has heard of. Caught by the caller,
            which can then list what there is.
    """
    if name is None:
        return None
    return HUB_TYPES[name.strip().lower()]


@firmware_app.command(name="list")
async def firmware_list(
    *,
    hub_type: str | None = None,
    only_cached: bool = False,
    refresh: bool = False,
    allow_prerelease: AllowPrerelease = False,
    verbose: Verbose = (),
) -> int:
    """Show the firmware that can be installed.

    Everything published, with the versions already on this computer marked,
    so there is one list to read rather than two to hold in your head.

    Args:
        hub_type: Only show firmware for one kind of hub, such as cityhub.
        only_cached: Show only what is already here, without going to the
            network. Faster, works offline, and answers "what do I have".
        refresh: Check for new releases even if the last look was recent.
        allow_prerelease: Include beta versions.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_list, hub_type, only_cached, refresh, allow_prerelease)


async def _list(
    hub_type: str | None, only_cached: bool, refresh: bool, allow_prerelease: bool
) -> int:
    out, err = ui.console(), ui.console(err=True)
    try:
        wanted = _hub_type(hub_type)
    except KeyError:
        return ui.usage(
            f"There is no hub type called {hub_type!r}.",
            f"Try one of: {', '.join(sorted(HUB_TYPES))}.",
        )

    async with pb_client.connect() as pb:
        catalogue = await pb.firmware_catalogue(
            hub_type=wanted, cached_only=only_cached, refresh=refresh
        )

    releases = [
        release
        for release in catalogue.releases
        if allow_prerelease or not release.prerelease
    ]

    if out.is_terminal:
        out.print(ui.firmware_table(releases, catalogue.cached, HUB_LABELS))
    else:
        # Tab separated, so cut and awk work on it. The columns are what a
        # script would branch on: the version, whether it is a beta, whether
        # it is here already, and which hubs it covers.
        names = {value: key for key, value in HUB_TYPES.items()}
        for release in releases:
            kind = "beta" if release.prerelease else "released"
            here = "cached" if release.cached else "-"
            hubs = ",".join(names.get(hub, str(hub)) for hub in release.hubs)
            ui.plain(out, f"{release.version}\t{kind}\t{here}\t{hubs}")

    if not catalogue.reached_index:
        # Said rather than implied: the list is real, it is just not current.
        err.print("[yellow]Could not check for new firmware.[/]")
        err.print("This is what is already on this computer.", style="dim")
    return EXIT_OK


@firmware_app.command(name="fetch")
async def firmware_fetch(
    version: str | None = None,
    /,
    *,
    hub_type: str,
    allow_prerelease: AllowPrerelease = False,
    verbose: Verbose = (),
) -> int:
    """Download a firmware image onto this computer.

    Not onto a hub: that is what flashing does. Fetching one first is only
    needed to work offline afterwards, since installing fetches what it needs.

    Args:
        version: Which version, exactly or in part -- 4.1 means the newest of
            those. Left out means the newest there is.
        hub_type: Which hub the firmware is for, such as cityhub.
        allow_prerelease: Consider beta versions.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_fetch, version, hub_type, allow_prerelease)


async def _fetch(version: str | None, hub_type: str, allow_prerelease: bool) -> int:
    err = ui.console(err=True)
    try:
        wanted = _hub_type(hub_type)
    except KeyError:
        return ui.usage(
            f"There is no hub type called {hub_type!r}.",
            f"Try one of: {', '.join(sorted(HUB_TYPES))}.",
        )
    assert wanted is not None

    outcome: pb_client.Fetched | None = None
    async with pb_client.connect() as pb:
        with ui.progress(err, "Downloading firmware") as bar:
            task = bar.add_task("Downloading firmware", total=None)
            async with pb.fetching_firmware(
                hub_type=wanted, version=version, allow_prerelease=allow_prerelease
            ) as events:
                async for event in events:
                    if isinstance(event, pb_client.Progress):
                        bar.update(
                            task, completed=event.done, total=event.total or None
                        )
                    elif isinstance(event, pb_client.Fetched):
                        outcome = event

    if outcome is None:  # pragma: no cover - the service always answers
        return errors.EXIT_ERROR

    if outcome.cached:
        err.print(f"Firmware {outcome.version} was already here.")
    else:
        err.print(f"Downloaded firmware {outcome.version} ({outcome.size} bytes).")
    return EXIT_OK


@firmware_app.command(name="clean")
async def firmware_clean(
    version: str | None = None,
    /,
    *,
    hub_type: str | None = None,
    all: bool = False,
    verbose: Verbose = (),
) -> int:
    """Remove downloaded firmware from this computer.

    Only from this computer. Taking Pybricks off a hub is a different thing
    and will be called restore.

    Args:
        version: Which version to remove.
        hub_type: Only remove firmware for one kind of hub.
        all: Remove all of it, rather than one version.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    if version is None and not all:
        return ui.usage(
            "Say which version to remove.",
            "Name a version, or pass [bold]--all[/] to empty the cache.",
        )
    if version is not None and all:
        return ui.usage(
            "Remove one version or all of them, not both.",
            "Leave the version out if you meant [bold]--all[/].",
        )
    return await errors.run(_clean, version, hub_type, all)


async def _clean(version: str | None, hub_type: str | None, everything: bool) -> int:
    err = ui.console(err=True)
    try:
        wanted = _hub_type(hub_type)
    except KeyError:
        return ui.usage(
            f"There is no hub type called {hub_type!r}.",
            f"Try one of: {', '.join(sorted(HUB_TYPES))}.",
        )

    async with pb_client.connect() as pb:
        removed = await pb.clean_firmware(
            hub_type=wanted, version=version, everything=everything
        )

    if not removed.removed:
        err.print("Nothing to remove.")
        return EXIT_OK

    freed = sum(image.size for image in removed.removed)
    err.print(f"Removed {len(removed.removed)} archive(s), freeing {freed} bytes.")
    return EXIT_OK


@firmware_app.command(name="dir")
async def firmware_dir(*, verbose: Verbose = ()) -> int:
    """Print where downloaded firmware is kept.

    An ordinary directory of ordinary files: a zip downloaded on another
    computer can be copied in by hand and will be found, which is how this
    works with no network at all.

    Args:
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_dir)


async def _dir() -> int:
    out = ui.console()
    async with pb_client.connect() as pb:
        catalogue = await pb.firmware_catalogue(cached_only=True)
    # The path is the answer to the question, so it is data and goes to
    # stdout on its own -- `cd "$(pb firmware dir)"` should work.
    ui.plain(out, catalogue.cache_dir)
    return EXIT_OK
