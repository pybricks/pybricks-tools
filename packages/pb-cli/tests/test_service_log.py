# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Reading the service's log, which is the only place it can speak from.

The command never contacts the service -- it reads a file -- so everything
here is about that file: how much of it to show, what to do when it is not
there, and keeping up with one that is still being written.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import pytest
from rich.console import Console

from pb_cli import errors
from pb_cli.commands import service

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the command at a log file of the test's own."""
    path = tmp_path / "pb-service.log"
    monkeypatch.setattr(service, "log_file", lambda: path)
    return path


SETTLED = 0.3
"""How long to leave between writes that must be reported separately.

Comfortably more than the watcher's debounce, so that two writes cannot be
gathered into one report -- which would make the test pass without the code
holding a part-line back, since it would never see one.
"""

PATIENCE = 20.0
"""How long to wait for what was written to arrive.

Generous, because it is not measuring anything: the test ends as soon as the
lines turn up, and this only decides how long a broken one takes to fail.
"""


async def _followed(
    log: Path, offset: int, write: Callable[[], Awaitable[None]], want: list[str]
) -> list[str]:
    """Follow the log while something writes to it, and say what was printed.

    Args:
        log: The file to follow.
        offset: Where to start from.
        write: Does the writing, once following has started.
        want: The lines expected, which is what ends the wait.

    Returns:
        The lines that were printed.
    """
    arrived = anyio.Event()
    buffer = _Expecting(want, arrived)
    out = Console(file=buffer, highlight=False)

    with anyio.move_on_after(PATIENCE):
        async with anyio.create_task_group() as tg:
            # A test may reach into the module it is testing.
            tg.start_soon(
                service._follow,  # pyright: ignore[reportPrivateUsage]
                out,
                log,
                offset,
            )
            await write()
            await arrived.wait()
            tg.cancel_scope.cancel()

    return buffer.getvalue().splitlines()


class _Expecting(io.StringIO):
    """Somewhere to print that says when what was expected has been printed.

    Waiting on this rather than looking at it every so often, because a test
    for code that waits should not itself poll -- and because a test that ends
    the moment the lines arrive is both quicker and less arbitrary than one
    that ends after a chosen number of periods.
    """

    def __init__(self, want: list[str], arrived: anyio.Event) -> None:
        """Record what to wait for.

        Args:
            want: The lines that mean the test can stop.
            arrived: Set once they have all been printed.
        """
        super().__init__()
        self._want = want
        self._arrived = arrived

    def write(self, s: str, /) -> int:
        """Take what was printed, and notice if it completes the set."""
        written = super().write(s)

        if self.getvalue().splitlines() == self._want:
            self._arrived.set()

        return written


def _write(path: Path, text: str) -> None:
    """Put a file there. Sync, because arranging one is not event-loop work."""
    path.write_text(text, encoding="utf-8")


def _append(path: Path, text: str) -> None:
    """Add to a file the way the service adds to its log."""

    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _size(path: Path) -> int:
    """How long the file is now."""
    return path.stat().st_size


async def test_the_last_lines_go_to_stdout(
    log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(log, "".join(f"line {n}\n" for n in range(100)))

    assert await service.service_log(lines=3) == errors.EXIT_OK

    captured = capsys.readouterr()
    # Data, so stdout, and exactly what the file says.
    assert captured.out.splitlines() == ["line 97", "line 98", "line 99"]
    assert captured.err == ""


async def test_no_lines_at_all_means_the_whole_file(
    log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(log, "first\nsecond\nthird\n")

    assert await service.service_log(lines=0) == errors.EXIT_OK

    assert capsys.readouterr().out.splitlines() == ["first", "second", "third"]


async def test_a_log_that_does_not_exist_yet_is_an_answer(
    log: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Not a failure: the service has not run since this machine last tidied up
    # after it, which is worth saying rather than raising.
    assert await service.service_log(lines=10) == errors.EXIT_OK

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "has not written a log yet" in captured.err


async def test_only_the_end_of_a_large_log_is_read(
    log: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file rotates at 2 MiB, so answering "what happened just now" must not
    # mean reading all of it. What matters is that the saving never costs half
    # a line.
    monkeypatch.setattr(service, "TAIL_BYTES", 64)
    _write(log, "".join(f"a line numbered {n}\n" for n in range(100)))

    assert await service.service_log(lines=2) == errors.EXIT_OK

    printed = capsys.readouterr().out.splitlines()
    assert printed == ["a line numbered 98", "a line numbered 99"]


async def test_following_prints_what_arrives(log: Path) -> None:
    _write(log, "before\n")

    async def append() -> None:
        for line in ("during\n", "after\n"):
            await anyio.sleep(SETTLED)
            _append(log, line)

    printed = await _followed(log, _size(log), append, ["during", "after"])

    assert printed == ["during", "after"]


async def test_following_survives_the_log_being_rotated(log: Path) -> None:
    # The service rotates its log at 2 MiB. Somebody watching it should not
    # have to notice, and above all should not sit silent afterwards because
    # the position they held is now past the end of a smaller file.
    _write(log, "".join(f"old {n}\n" for n in range(50)))
    start = _size(log)

    async def rotate() -> None:
        await anyio.sleep(SETTLED)
        _write(log, "brand new\n")

    printed = await _followed(log, start, rotate, ["brand new"])

    assert printed == ["brand new"]


async def test_a_line_split_across_two_reads_is_printed_once(log: Path) -> None:
    # A log line is written by another process, so a read can land in the
    # middle of one. Printing half of it and then the other half would make
    # the output unreadable exactly when somebody is reading it closely.
    _write(log, "")

    async def dribble() -> None:
        for piece in ("half a ", "line\n"):
            await anyio.sleep(SETTLED)
            _append(log, piece)

    printed = await _followed(log, 0, dribble, ["half a line"])

    assert printed == ["half a line"]
