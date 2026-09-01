# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The POSIX terminal, in raw mode.

Waiting is the event loop's own job here: a terminal is a file descriptor, so
``anyio.wait_readable`` registers it with the selector and the task is woken
when a key is pressed. No thread, no timer, and cancellation lands where the
task is waiting rather than being deferred to the end of some interval.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from collections.abc import Generator
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from pb_cli.console import Console

__all__ = ["platform_problem", "raw_console"]

READ_LIMIT = 4096
"""Bytes to take from the terminal in one go.

Well past what typing produces and enough that pasting a block of code arrives
in a couple of reads rather than a hundred.
"""


def platform_problem() -> str | None:
    """Whether a terminal can be put into raw mode.

    Returns:
        None. On POSIX ``isatty`` means what it says, and the caller has
        already asked it.
    """
    return None


class _PosixConsole:
    """A POSIX terminal, in raw mode."""

    def __init__(self, in_fd: int, out_fd: int) -> None:
        """Hold the descriptors raw mode was set on.

        Args:
            in_fd: The terminal to read from.
            out_fd: The terminal to write to.
        """
        self._in = in_fd
        self._out = out_fd

    async def wait(self) -> None:
        """Wait until the terminal has input."""
        await anyio.wait_readable(self._in)

    def read(self) -> bytes:
        """Take whatever has been typed.

        Returns:
            The bytes typed, or empty.
        """
        import select

        readable, _, _ = select.select([self._in], [], [], 0)
        if not readable:
            return b""

        return os.read(self._in, READ_LIMIT)

    def write(self, data: bytes) -> None:
        """Write bytes to the terminal, byte for byte.

        Args:
            data: What to write.
        """

        while data:
            data = data[os.write(self._out, data) :]

    def size(self) -> tuple[int, int]:
        """Return the terminal's size.

        Returns:
            Columns and rows.
        """
        columns, lines = shutil.get_terminal_size()

        return columns, lines


@contextlib.contextmanager
def raw_console() -> Generator[Console]:
    """Put the terminal into raw mode for the duration of the block.

    Raw rather than cbreak, unlike :mod:`pb_cli.keys`: the interpreter on the
    hub needs Ctrl-C as a byte, so the kernel must not turn it into a signal.
    Output processing goes too, which is why everything written while this is
    held ends its lines with a carriage return of its own.

    Yields:
        The terminal.
    """
    import termios
    import tty

    in_fd = sys.stdin.fileno()
    out_fd = sys.stdout.fileno()
    saved = termios.tcgetattr(in_fd)

    try:
        tty.setraw(in_fd)

        yield _PosixConsole(in_fd, out_fd)

    finally:
        # TCSADRAIN so that anything already written is not cut off. A
        # synchronous exit, so no cancellation can skip it.
        termios.tcsetattr(in_fd, termios.TCSADRAIN, saved)
