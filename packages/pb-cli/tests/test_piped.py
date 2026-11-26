# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Piped standard input: telling what stdin is, and translating line endings.

The whole path, a pipe through ``pb run`` to a hub, is tested end to end in
``tests/e2e/test_repl.py``. These are the two pieces of it that are decisions
rather than plumbing.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import anyio
import anyio.to_thread
import pytest

from pb_cli import piped, session

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(
    ("reads", "expected"),
    [
        ([b"a\nb\n"], b"a\rb\r"),
        ([b"a\r\nb\r\n"], b"a\rb\r"),
        ([b"a\rb\r"], b"a\rb\r"),
        # A \r\n split between two reads is still one line ending, not two.
        ([b"a\r", b"\nb"], b"a\rb"),
        ([b"a\r", b"\n", b"b\n"], b"a\rb\r"),
        # A blank line is still a line.
        ([b"\n\n"], b"\r\r"),
        ([b"\r", b"\r\n"], b"\r\r"),
    ],
)
def test_line_endings_become_what_the_hub_ends_a_line_on(
    reads: list[bytes], expected: bytes
) -> None:
    after_cr = False
    out = b""

    for data in reads:
        translated, after_cr = session._line_endings(  # pyright: ignore[reportPrivateUsage]
            data, after_cr
        )
        out += translated

    assert out == expected


def test_stdin_that_pytest_has_replaced_is_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Something with no descriptor behind it, as under pytest or an embedding.
    monkeypatch.setattr(sys, "stdin", io.StringIO("not for the hub"))
    assert piped.piped_stdin() is None


@pytest.fixture
def stdin_from(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Make standard input a file or device, the way ``pb run < path`` does.

    Yields:
        A function that opens a path as stdin, closed again afterwards.
    """
    with contextlib.ExitStack() as opened:

        def use(path: str) -> None:
            monkeypatch.setattr(
                sys, "stdin", opened.enter_context(Path(path).open("rb"))
            )

        yield use


@pytest.mark.skipif(sys.platform == "win32", reason="NUL counts as a terminal there")
async def test_dev_null_ends_at_once(stdin_from: Callable[[str], None]) -> None:
    # What stdin is under most service managers and CI runners. epoll refuses
    # it, so it is found to be always ready, and reads as the end straight
    # away -- rather than being waited on for ever.
    stdin_from(os.devnull)
    source = piped.piped_stdin()
    assert source is not None

    with anyio.fail_after(5):
        assert await source.read() == b""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX devices")
async def test_a_device_that_cannot_be_waited_on_is_read_like_a_file(
    stdin_from: Callable[[str], None],
) -> None:
    # /dev/zero never ends, which is fine: each write to the hub waits for the
    # program to read, so it goes no faster than the program takes it, exactly
    # as cat /dev/zero | pb run would.
    stdin_from("/dev/zero")
    source = piped.piped_stdin()
    assert source is not None

    with anyio.fail_after(5):
        for _ in range(3):
            assert await source.read() == bytes(piped.READ_LIMIT)


@pytest.mark.skipif(sys.platform != "win32", reason="overlapped handles are Windows")
def test_an_overlapped_pipe_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # A synchronous read on an overlapped handle can report completion at
    # once, so the zero-byte wait would spin. Such a handle is left alone.
    import ctypes
    import ctypes.wintypes as wintypes
    import msvcrt
    import uuid

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateNamedPipeW
    create.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    )
    create.restype = wintypes.HANDLE
    pipe_access_inbound = 0x00000001
    file_flag_overlapped = 0x40000000

    handle = create(
        rf"\\.\pipe\pb-piped-{uuid.uuid4().hex}",
        pipe_access_inbound | file_flag_overlapped,
        0,
        1,
        0,
        0,
        0,
        None,
    )
    assert handle not in (None, ctypes.c_void_p(-1).value)

    with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        assert piped.piped_stdin() is None


class _Broken:
    async def read(self) -> bytes:
        raise ConnectionResetError("the other end went away")


async def test_a_stdin_that_fails_is_not_swallowed() -> None:
    # Every error reading stdin means something is broken rather than
    # finished. Carrying on would leave the program waiting in input() for
    # what can no longer come, so the error ends the session -- which stops the
    # program on the way out.
    arrived = anyio.Event()
    arrived.set()

    with pytest.raises(ConnectionResetError), anyio.fail_after(5):
        # No service: nothing reaches the point of writing to one.
        await session._forward_piped(  # pyright: ignore[reportPrivateUsage]
            None,  # pyright: ignore[reportArgumentType]
            None,
            _Broken(),
            arrived,
            None,  # pyright: ignore[reportArgumentType]
        )


@pytest.fixture
def pipe(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    read_fd, write_fd = os.pipe()

    with os.fdopen(read_fd, "rb", buffering=0) as reader:
        monkeypatch.setattr(sys, "stdin", reader)
        yield write_fd


async def test_a_pipe_is_read_until_its_writer_closes(pipe: int) -> None:
    source = piped.piped_stdin()
    assert source is not None

    os.write(pipe, b"hello\n")
    os.close(pipe)

    received = b""
    with anyio.fail_after(5):
        while data := await source.read():
            received += data

    assert received == b"hello\n"


async def test_waiting_on_an_empty_pipe_can_be_cancelled(pipe: int) -> None:
    source = piped.piped_stdin()
    assert source is not None

    try:
        with anyio.move_on_after(0.2) as scope:
            await source.read()

        assert scope.cancelled_caught

        # And cancelling the wait took nothing and broke nothing: the next
        # thing written is the next thing read.
        os.write(pipe, b"after")
        with anyio.fail_after(5):
            assert await source.read() == b"after"
    finally:
        os.close(pipe)


async def test_a_wait_already_in_progress_wakes_for_what_arrives(pipe: int) -> None:
    source = piped.piped_stdin()
    assert source is not None
    received: list[bytes] = []

    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:

                async def take() -> None:
                    received.append(await source.read())

                tg.start_soon(take)
                # Long enough for the read to be waiting, not just about to.
                await anyio.sleep(0.1)
                assert not received

                os.write(pipe, b"late")
    finally:
        os.close(pipe)

    assert received == [b"late"]


async def test_cancelling_at_every_moment_never_hangs(pipe: int) -> None:
    # Cancellation can land before the waiting thread starts, while it is
    # about to read, or while it is in the read. Each needs handling, and a
    # mistake in any of them is a hang, so try a spread of moments.
    source = piped.piped_stdin()
    assert source is not None

    try:
        with anyio.fail_after(20):
            for i in range(200):
                with anyio.move_on_after((i % 20) / 10_000):
                    await source.read()
    finally:
        os.close(pipe)


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows waiting thread")
async def test_a_windows_wait_does_not_spin(
    pipe: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The wait is a read that blocks. If it ever stopped blocking -- returned
    # at once on an empty pipe -- read() would loop on it at full speed, which
    # is the poll this design exists to avoid.
    from pb_cli import _piped_windows

    calls = 0
    block = _piped_windows._ZeroReadPipe._block  # pyright: ignore[reportPrivateUsage]

    def counted(self: _piped_windows._ZeroReadPipe) -> None:  # pyright: ignore[reportPrivateUsage]
        nonlocal calls
        calls += 1
        block(self)

    monkeypatch.setattr(_piped_windows._ZeroReadPipe, "_block", counted)  # pyright: ignore[reportPrivateUsage]
    source = piped.piped_stdin()
    assert source is not None

    try:
        with anyio.move_on_after(0.3):
            await source.read()
    finally:
        os.close(pipe)

    assert calls == 1


async def test_a_file_is_read_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "answers.txt"
    path.write_bytes(b"x" * (piped.READ_LIMIT + 10))

    with path.open("rb") as file:
        monkeypatch.setattr(sys, "stdin", file)
        source = piped.piped_stdin()
        assert source is not None

        received = b""
        while data := await source.read():
            received += data

    assert received == path.read_bytes()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows waiting thread")
async def test_a_cancel_that_hits_other_io_still_releases_the_wait(
    pipe: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Between saying it is about to read and reading, the waiting thread may
    # do some other synchronous I/O -- a finalizer writing to a full stderr
    # pipe, say. A cancel landing then cancels that instead and reports
    # success, and the read starts afterwards with nothing to cancel it. The
    # release has to keep going until the thread is past the read.
    import threading
    import time

    from pb_cli import _piped_windows

    side_read, side_write = os.pipe()
    real_read_file = _piped_windows._ReadFile  # pyright: ignore[reportPrivateUsage]
    real_cancel = _piped_windows._CancelSynchronousIo  # pyright: ignore[reportPrivateUsage]
    detoured = threading.Event()
    first_cancel_done = threading.Event()

    def cancel(handle: int) -> int:
        result = real_cancel(handle)
        first_cancel_done.set()
        return result

    def read_file_after_other_io(*args: object) -> object:
        if not detoured.is_set():
            detoured.set()
            # Blocks, and is what the first cancel finds.
            with contextlib.suppress(OSError):
                os.read(side_read, 1)

            # Not straight on into the read: Windows itself can make the
            # cancel call wait for a read started the instant the cancelled
            # one ends -- measured, with plain threads and none of this code
            # -- which is a different race from the one under test. Waiting
            # for that call to return gives the old logic exactly one cancel,
            # spent on the detour, and nothing to stop the read that follows.
            first_cancel_done.wait(5)

        return real_read_file(*args)

    monkeypatch.setattr(_piped_windows, "_ReadFile", read_file_after_other_io)
    monkeypatch.setattr(_piped_windows, "_CancelSynchronousIo", cancel)
    source = piped.piped_stdin()
    assert source is not None

    def rescue() -> None:
        # A regression is a hang inside a shielded thread wait, which no
        # timeout can interrupt, so it is ended from outside instead.
        if not finished.wait(3):
            os.write(pipe, b"rescued")

    finished = threading.Event()
    threading.Thread(target=rescue, daemon=True).start()

    try:
        started = time.monotonic()
        async with anyio.create_task_group() as tg:
            tg.start_soon(source.read)
            # Bounded, because this wait is shielded: if the detour is never
            # reached, an unbounded one would hang the whole suite.
            assert await anyio.to_thread.run_sync(detoured.wait, 5)
            await anyio.sleep(0.1)
            tg.cancel_scope.cancel()

        finished.set()
        assert time.monotonic() - started < 2
    finally:
        finished.set()
        os.close(pipe)
        os.close(side_read)
        os.close(side_write)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows pipe handles")
def test_the_write_end_of_a_pipe_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # pb run x.py 0<&1 | more: stdin is a pipe, but not one that can be read.
    read_fd, write_fd = os.pipe()

    with os.fdopen(write_fd, "wb", buffering=0) as wrong_end:
        monkeypatch.setattr(sys, "stdin", wrong_end)
        assert piped.piped_stdin() is None

    os.close(read_fd)
