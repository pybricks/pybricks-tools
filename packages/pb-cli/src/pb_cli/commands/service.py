# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Managing the background service that keeps hubs connected.

Rarely needed: the service starts by itself when something wants it and stops
when nothing has for a while. These exist for the times it has to be looked at
directly -- after an upgrade, or when a hub is behaving oddly and the log is
the only place the reason will be.
"""

from __future__ import annotations

import contextlib
import logging
from contextlib import aclosing
from pathlib import Path
from typing import Annotated, NamedTuple

import anyio
import cyclopts
from rich.console import Console

# The unknown here is watchfiles' own AnyEvent union, which names trio's
# Event as one of the things a stop_event may be. trio is not installed and
# never will be -- bleak pins this to asyncio -- and nothing here passes a
# stop_event at all, since aclosing is what stops the watch.
from watchfiles import awatch  # pyright: ignore[reportUnknownVariableType]

import pb_client
from pb_cli import errors, ui
from pb_cli.app import Verbose, configure_logging, service_app
from pb_cli.errors import EXIT_OK
from pb_core.paths import log_file

logger = logging.getLogger(__name__)

__all__ = [
    "service_log",
    "service_start",
    "service_status",
    "service_stop",
]

_HANDLED_HERE = (pb_client.PbClientError, BaseExceptionGroup)
"""What to catch when a command deals with a failure itself.

Catching the error's own type is not enough: anything raised inside an
``async with pb_client.connect()`` block comes back wrapped in a group of one,
so the type to catch is not the type that was raised.
"""


@service_app.command(name="status")
async def service_status(*, verbose: Verbose = (), start: bool = False) -> int:
    """Show whether the background service is running.

    Args:
        verbose: Increase log detail.
        start: Start the service if it is not running, instead of reporting
            that it is not.
    """
    configure_logging(verbose)
    return await errors.run(_service_status, start)


async def _service_status(start: bool) -> int:
    out, err = ui.console(), ui.console(err=True)

    try:
        async with pb_client.connect(autostart=start) as service:
            info = await service.info()
    except _HANDLED_HERE as exc:
        # "It is not running" is the answer to the question, not a failure to
        # answer it, so it is reported here rather than as an error.
        if errors.find(exc, pb_client.ServiceUnavailable) is None:
            raise

        err.print("[yellow]Not running.[/]")
        err.print("It starts by itself when needed, or run [bold]pb service start[/].")
        return errors.EXIT_NOTHING_CONNECTED

    ui.rows(
        out,
        [
            ("status", "running"),
            ("version", info.version),
            ("protocol", str(info.protocol_version)),
            ("pid", str(info.pid)),
            ("uptime_s", f"{info.uptime_s:.1f}"),
            ("endpoint", info.endpoint),
            ("clients", str(info.clients)),
            ("log", info.log_file),
        ],
    )
    return EXIT_OK


@service_app.command(name="start")
async def service_start(*, verbose: Verbose = ()) -> int:
    """Start the background service if it is not already running.

    Args:
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_service_start)


async def _service_start() -> int:
    err = ui.console(err=True)

    async with pb_client.connect(autostart=True) as service:
        info = await service.info()

    err.print(f"Running (pid {info.pid}).")
    return EXIT_OK


@service_app.command(name="stop")
async def service_stop(*, verbose: Verbose = ()) -> int:
    """Stop the background service, disconnecting any hubs.

    Args:
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_service_stop)


async def _service_stop() -> int:
    err = ui.console(err=True)

    try:
        async with pb_client.connect(autostart=False) as service:
            await service.shutdown()
    except _HANDLED_HERE as exc:
        # Stopping something that is already stopped succeeded.
        if errors.find(exc, pb_client.ServiceUnavailable) is None:
            raise

        err.print("Not running.")
        return EXIT_OK

    err.print("Stopping.")
    return EXIT_OK


TAIL_BYTES = 1024 * 1024
"""How much of the end of the log to read when only the last lines are wanted.

The file rotates at 2 MiB, so this is most of one, and it saves reading all of
a large file to answer a question about its last few lines.
"""

FOLLOW_STEP_MS = 20
"""How long the watcher waits for quiet before reporting what it has seen.

The pause after a burst of writing, so a line reaches the screen about as fast
as the terminal can draw it.
"""

FOLLOW_DEBOUNCE_MS = 100
"""The longest the watcher groups changes before reporting them anyway.

A log being written steadily never goes quiet for :data:`FOLLOW_STEP_MS`, so
without this it would be reported only when the writing paused. The library's
own default is 1600 ms, which is fine for rebuilding a project and far too slow
for watching output arrive.
"""


@service_app.command(name="log")
async def service_log(
    *,
    lines: int = 50,
    follow: Annotated[bool, cyclopts.Parameter(name=["-f", "--follow"])] = False,
    verbose: Verbose = (),
) -> int:
    """Show what the background service has been doing.

    The service has no terminal of its own, so its log is the only place its
    side of the story is written down. This reads the file directly and never
    contacts the service, which means it still works when the service is not
    running -- which is when it is most likely to be wanted.

    Args:
        lines: How many lines from the end to show. Zero shows the whole file.
        follow: Keep watching, printing new lines as they arrive. Ctrl-C
            leaves.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_service_log, lines, follow)


async def _service_log(lines: int, follow: bool) -> int:
    out, err = ui.console(), ui.console(err=True)
    path = log_file()
    if not path.is_file():
        # Nothing written is an answer rather than a failure: the service has
        # not run since this machine last tidied up after it.
        err.print("The background service has not written a log yet.")
        err.print("It starts by itself when needed, or run [bold]pb service start[/].")
        return EXIT_OK

    offset = _print_the_end(out, path, lines)

    if not follow:
        return EXIT_OK

    err.print(f"Watching [bold]{path}[/]. Ctrl-C to stop.")

    with contextlib.suppress(KeyboardInterrupt):
        # Caught rather than left to the top level: Ctrl-C is how this command
        # is meant to end, so it is not an interruption of anything.
        await _follow(out, path, offset)

    return EXIT_OK


def _print_the_end(out: Console, path: Path, lines: int) -> int:
    """Print the last of the log, and say where reading reached.

    Args:
        out: Where the lines go.
        path: The log file.
        lines: How many to print, or zero for all of them.

    Returns:
        The offset reading reached, for following on from.
    """
    end = path.stat().st_size
    start = max(0, end - TAIL_BYTES) if lines > 0 else 0

    with path.open("rb") as handle:
        handle.seek(start)
        block = handle.read()

    if start:
        # The seek lands in the middle of a line, and half a line is worse
        # than one line fewer.
        block = block.split(b"\n", 1)[-1]

    text = _decode(block).splitlines()

    for line in text[-lines:] if lines > 0 else text:
        ui.plain(out, line)

    return end


async def _follow(out: Console, path: Path, offset: int) -> None:
    """Print what is added to the log, until the caller is interrupted.

    Waits to be told rather than asking. The directory is watched rather than
    the file, because a tail has two questions and only one of them is about
    the file it is holding: the log rotates at 2 MiB, and the replacement
    arriving is a change to the directory.

    Args:
        out: Where the lines go.
        path: The log file.
        offset: Where reading has already reached.
    """
    # anyio's Path, not pathlib's: this runs for as long as somebody watches,
    # and a file system that has gone slow -- a network drive, a sleeping disk
    # -- must not stall the event loop it is running in.
    watched = anyio.Path(path)
    state = _Tail(offset=offset)
    # Once before waiting, since the service may have written something
    # between the tail above and the watch starting, and a line missed here
    # would never be reported by any later change.
    state = await _drain(out, watched, state)

    # aclosing because this generator holds a thread and the watcher behind
    # it, released when its stop event is set on the way out. Leaving on
    # Ctrl-C is the ordinary way this command ends, so that path is the one
    # that has to be tidy. See the async rules in CONTRIBUTING.md.
    async with aclosing(
        awatch(
            path.parent,
            # Every change, rather than the library's default filter, which
            # exists for source trees and has opinions about file names.
            watch_filter=None,
            recursive=False,
            step=FOLLOW_STEP_MS,
            debounce=FOLLOW_DEBOUNCE_MS,
            # Overriding watchfiles' own choice, deliberately and with a
            # measurement behind it. It turns polling on automatically for the
            # whole of WSL, because inotify does not work for files on a
            # Windows drive mounted through drvfs -- but its polling reports
            # *nothing at all* for a file on tmpfs, measured on WSL2 with
            # Ubuntu 26.04: three appends over a second, and no event in the
            # three seconds that followed. The same watch with this set to
            # False reported every one of them 20 ms after it happened. The
            # log lives in the state directory, which on WSL is inside the
            # Linux file system where inotify works. A log on a Windows drive
            # is the case this gives up on, and it is the case where the
            # alternative reports nothing anyway.
            force_polling=False,
        )
    ) as changes:
        async for _ in changes:
            state = await _drain(out, watched, state)


class _Tail(NamedTuple):
    """Where reading has reached, and the part of a line still waiting."""

    offset: int
    """How much of the file has been read."""

    pending: bytes = b""
    """Bytes after the last newline, held back until the rest arrives."""


async def _drain(out: Console, watched: anyio.Path, state: _Tail) -> _Tail:
    """Print whatever has been added since the last read.

    Args:
        out: Where the lines go.
        watched: The log file.
        state: Where reading had reached.

    Returns:
        Where reading has reached now.
    """

    try:
        size = (await watched.stat()).st_size
    except FileNotFoundError:
        # Rotated away, and the replacement is not there yet.
        return _Tail(offset=0)

    if size < state.offset:
        # Rotated: what is here now is a new file, so a position from the old
        # one means nothing.
        state = _Tail(offset=0)

    if size == state.offset:
        return state

    async with await watched.open("rb") as handle:
        await handle.seek(state.offset)
        block = await handle.read()

    # A line can arrive in two pieces, so hold back anything after the last
    # newline until the rest of it turns up.
    complete, _, pending = (state.pending + block).rpartition(b"\n")

    for line in _decode(complete).splitlines():
        ui.plain(out, line)

    return _Tail(offset=state.offset + len(block), pending=pending)


def _decode(block: bytes) -> str:
    """Turn log bytes into text without ever failing on them.

    Args:
        block: What was read.

    Returns:
        The text. The service writes UTF-8, but a file cut mid-character by
        rotation, or written by an older build, has to stay readable rather
        than raise.
    """
    return block.decode("utf-8", "replace")
