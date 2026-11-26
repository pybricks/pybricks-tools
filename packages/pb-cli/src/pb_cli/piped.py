# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

r"""Standard input that is not a terminal, read without ever blocking the loop.

``printf 'a\n' | pb run prog.py`` and ``pb run prog.py < answers.txt`` should
hand the program on the hub what was piped in, the way ``ssh`` or
``docker run -i`` would. That is a different job from :mod:`pb_cli.console`:
no raw mode, no keys to sort, just bytes -- but the same rule about waiting.
Every read here either waits for the platform to say there is input or cannot
block in the first place, so it can always be cancelled, and nothing is left
behind in a thread when the program ends before the input does.

What kind of thing stdin is decides how:

- **A pipe, socket or device on POSIX** is a file descriptor the event loop
  can wait on, so it does. A device the selector refuses, such as
  ``/dev/null``, is always ready, and is then read like a file.
- **A regular file**, on either platform, never blocks on a read, and is read
  straight through. (POSIX selectors refuse regular files outright, which is
  why this is told apart rather than tried.)
- **A pipe on Windows** has nothing an event loop will wait on, so a worker
  thread waits instead, in a zero-byte read that takes nothing and can be
  cancelled. See :mod:`pb_cli._piped_windows`.
- **A terminal** is left alone. Reading one in cooked mode from a background
  job stops the whole process with ``SIGTTIN``, and a command run with its
  output redirected is often exactly that.
"""

from __future__ import annotations

import os
import stat
import sys
from typing import Final, Protocol

import anyio
import anyio.lowlevel

__all__ = ["Piped", "piped_stdin"]

READ_LIMIT: Final = 4096
"""Bytes to take in one read.

The service breaks whatever it is given into what the hub accepts, so this only
bounds how much is held here at once.
"""


class Piped(Protocol):
    """Standard input, readable without blocking anything else."""

    async def read(self) -> bytes:
        """Wait for some input and take it.

        Returns:
            What arrived, or empty once there will never be any more.
        """
        ...


class _Ready:
    """Input that is always ready: a regular file, read straight through."""

    def __init__(self, fd: int) -> None:
        """Hold the descriptor.

        Args:
            fd: Standard input.
        """
        self._fd = fd

    async def read(self) -> bytes:
        """Take the next piece of the file.

        Returns:
            The bytes, or empty at the end.
        """
        # A read of a file does not wait for anything, but a large one read in
        # a loop would otherwise never give the rest of the session a turn.
        await anyio.lowlevel.checkpoint()
        return os.read(self._fd, READ_LIMIT)


class _Waitable:
    """A POSIX pipe, socket or device, waited on by the event loop."""

    def __init__(self, fd: int, *, device: bool) -> None:
        """Hold the descriptor.

        Args:
            fd: Standard input.
            device: Whether it is a character device, which the selector may
                refuse. A pipe or socket it may not: one refused would be a
                real error, and reading it as though ready could block.
        """
        self._fd = fd
        self._device = device
        self._always_ready = False
        """Set once the selector has refused the descriptor."""

    async def read(self) -> bytes:
        """Wait until there is input, then take it.

        Returns:
            The bytes, or empty once the writer has closed its end.
        """

        if self._always_ready:
            await anyio.lowlevel.checkpoint()
        else:
            try:
                await anyio.wait_readable(self._fd)
            except OSError:
                if not self._device:
                    raise

                # A device with no way to wait on it, such as /dev/null or
                # /dev/zero. select() calls those always readable, and they
                # are, so a read returns at once, the same as a file. Linux's
                # epoll refuses them with EPERM -- measured, for /dev/null,
                # /dev/zero and /dev/urandom -- and macOS's kqueue refuses some
                # /dev files with EINVAL, which is why libuv probes for that
                # and falls back to select() for them.
                self._always_ready = True
                await anyio.lowlevel.checkpoint()

        # Readable means a read returns at once: with data, or empty at EOF.
        return os.read(self._fd, READ_LIMIT)


def piped_stdin() -> Piped | None:
    """Find out whether standard input has anything to forward, and how.

    Returns:
        A reader for it, or None when there is no stdin, it is a terminal, or
        it is something this cannot read without blocking.
    """

    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        # No stdin at all (pythonw), or one replaced by something with no
        # descriptor behind it, as pytest does.
        return None

    if os.isatty(fd):
        return None

    if sys.platform == "win32":
        from pb_cli import _piped_windows

        return _piped_windows.reader(fd)

    mode = os.fstat(fd).st_mode

    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return _Waitable(fd, device=False)

    if stat.S_ISCHR(mode):
        # One that can be waited on is, and one that cannot -- /dev/null,
        # which is what stdin is under most service managers and CI runners --
        # is found out on the first wait.
        return _Waitable(fd, device=True)

    if stat.S_ISREG(mode):
        return _Ready(fd)

    return None


def ready(fd: int) -> Piped:
    """Read a descriptor that never makes a read wait.

    Args:
        fd: The descriptor.

    Returns:
        A reader for it.
    """
    return _Ready(fd)
