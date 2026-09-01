# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Reading the service's log, which is the only place it can speak from.

The command never contacts the service -- it reads a file -- so everything
here is about that file: how much of it to show, what to do when it is not
there, and keeping up with one that is still being written.
"""

from __future__ import annotations

import io
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
    """Point the command at a log file of the test's own, and hurry it along."""
    path = tmp_path / "pb-service.log"
    monkeypatch.setattr(service, "log_file", lambda: path)
    # A quarter of a second is right for a person watching and far too long to
    # wait for repeatedly in a test. What is being tested is what the loop does
    # each time round, not how often it goes round.
    monkeypatch.setattr(service, "POLL_SECONDS", 0.02)
    return path


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


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, highlight=False), buffer


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
    out, buffer = _console()

    async def append() -> None:
        for line in ("during\n", "after\n"):
            await anyio.sleep(service.POLL_SECONDS)
            _append(log, line)

        await anyio.sleep(service.POLL_SECONDS * 3)

    with anyio.move_on_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(append)

            with anyio.move_on_after(service.POLL_SECONDS * 12):
                # A test may reach into the module it is testing.
                await service._follow(out, log, _size(log))  # pyright: ignore[reportPrivateUsage]

            tg.cancel_scope.cancel()

    assert buffer.getvalue().splitlines() == ["during", "after"]


async def test_following_survives_the_log_being_rotated(log: Path) -> None:
    # The service rotates its log at 2 MiB. Somebody watching it should not
    # have to notice, and above all should not sit silent afterwards because
    # the position they held is now past the end of a smaller file.
    _write(log, "".join(f"old {n}\n" for n in range(50)))
    out, buffer = _console()
    start = _size(log)

    async def rotate() -> None:
        await anyio.sleep(service.POLL_SECONDS)
        _write(log, "brand new\n")
        await anyio.sleep(service.POLL_SECONDS * 3)

    with anyio.move_on_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(rotate)

            with anyio.move_on_after(service.POLL_SECONDS * 12):
                await service._follow(out, log, start)  # pyright: ignore[reportPrivateUsage]

            tg.cancel_scope.cancel()

    assert buffer.getvalue().splitlines() == ["brand new"]


async def test_a_line_split_across_two_reads_is_printed_once(log: Path) -> None:
    # A log line is written by another process, so a read can land in the
    # middle of one. Printing half of it and then the other half would make
    # the output unreadable exactly when somebody is reading it closely.
    _write(log, "")
    out, buffer = _console()

    async def dribble() -> None:
        for piece in ("half a ", "line\n"):
            await anyio.sleep(service.POLL_SECONDS * 2)
            _append(log, piece)

        await anyio.sleep(service.POLL_SECONDS * 3)

    with anyio.move_on_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(dribble)

            with anyio.move_on_after(service.POLL_SECONDS * 14):
                await service._follow(out, log, 0)  # pyright: ignore[reportPrivateUsage]

            tg.cancel_scope.cancel()

    assert buffer.getvalue().splitlines() == ["half a line"]
