# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Reading single keystrokes, for the few places that ask a person to choose.

Deliberately small, and not to be confused with :mod:`pb_cli.console`, which is
what a session with a hub reads its keyboard through: that one forwards every
byte a key produces, in raw mode, and getting it right is most of a project.
This one only has to tell up from down from Enter, which is enough for a
chooser.

Two properties make it much safer than it looks:

**Nothing blocks.** :func:`read_ready` returns immediately whether or not a key
is waiting, so the caller stays in its own event loop and can redraw a display
between polls. There is no reader thread to abandon and no uninterruptible read
to cancel, which is where the difficulty in this area really lives.

**Windows needs no mode change at all.** :mod:`msvcrt` reads the console input
buffer directly, so there is no console state to save and none to restore, and
therefore no way to leave the terminal wedged. Only POSIX needs cbreak mode,
and it is entered and left by a *synchronous* context manager, whose
``__exit__`` cannot be cancelled -- which is stronger than shielding.

Only the reading is per platform. Deciding what a keystroke means is shared,
and understands both grammars at once: Windows reports an arrow as a prefix
character followed by a scan code, while a terminal reports it as an escape
sequence. Neither can be mistaken for the other, so one function handles both
and can be tested on either platform.

Ctrl-C is left to the operating system rather than read as a byte. It arrives
as :class:`KeyboardInterrupt`, so the way out of a chooser works even if
everything else here is wrong.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Generator
from enum import Enum

__all__ = [
    "Key",
    "keys_available",
    "poll_key",
    "read_ready",
    "read_second",
    "reading_keys",
]


class Key(Enum):
    """The handful of keys a chooser cares about."""

    UP = "up"
    DOWN = "down"
    ENTER = "enter"
    CANCEL = "cancel"
    """Escape or ``q``. Ctrl-C arrives as ``KeyboardInterrupt`` instead."""

    OTHER = "other"
    """Anything else, reported so a caller can ignore it deliberately."""


_WINDOWS_PREFIXES = ("\x00", "\xe0")
"""What the console sends before the scan code of a special key."""

_WINDOWS_SCAN_CODES = {"H": Key.UP, "P": Key.DOWN}

_CSI_FINAL_BYTES = {"A": Key.UP, "B": Key.DOWN}
"""The last character of a terminal's escape sequence for the arrows."""


def keys_available() -> bool:
    """Whether single keystrokes can be read at all.

    Returns:
        True when standard input is a terminal this can read from. False for a
        pipe, a file, or a shell that emulates a terminal over pipes -- Git
        Bash's ``mintty`` being the one on hand, where a native Windows Python
        sees a pipe and no console. A caller must check this and say something
        useful rather than waiting for a keystroke that cannot arrive.
    """

    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):  # pragma: no cover - odd embeddings
        # A closed or replaced stdin, which is not a terminal either.
        return False

    return _is_console()


def poll_key() -> Key | None:
    """Read one keystroke if one is waiting.

    Returns:
        What was pressed, or ``None`` if nothing has been typed. Never blocks,
        so a caller can redraw a display between calls.
    """
    char = read_ready()
    if char is None:
        return None

    if char in _WINDOWS_PREFIXES:
        # A Windows special key, which arrives as a prefix and then its
        # scan code. read_second rather than read_ready: the two halves
        # are not both in the console buffer, so asking whether anything
        # is waiting gets the wrong answer. See read_second.
        return _WINDOWS_SCAN_CODES.get(read_second() or "", Key.OTHER)

    if char == "\x1b":
        # Escape either stands alone or opens a sequence such as "\x1b[A" for
        # the up arrow. Telling those apart is the classic trap here, and
        # polling answers it directly: if nothing more is waiting this
        # instant, the key really was Escape.
        if read_ready() != "[":
            return Key.CANCEL

        return _CSI_FINAL_BYTES.get(read_ready() or "", Key.OTHER)

    if char in ("\r", "\n"):
        return Key.ENTER

    if char in ("q", "Q"):
        return Key.CANCEL

    return Key.OTHER


if sys.platform == "win32":

    @contextlib.contextmanager
    def reading_keys() -> Generator[None]:
        """Do nothing, because Windows needs nothing done.

        Yields:
            Nothing. ``msvcrt`` reads the console buffer without changing the
            console mode, so there is no state to restore.
        """
        yield

    def read_ready() -> str | None:
        """Read one character if the console has one waiting.

        Returns:
            The character, or ``None`` if nothing has been typed.
        """
        import msvcrt

        return msvcrt.getwch() if msvcrt.kbhit() else None

    def read_second() -> str | None:
        """Read the second half of a special key, which is already held.

        A special key reaches a program as two characters: a prefix,
        and then the key's scan code. The runtime hands over the first
        and keeps the second, and -- measured, by writing a down arrow
        into a real console with ``WriteConsoleInputW`` -- ``kbhit``
        then says nothing is waiting, because nothing is: the second
        half is in the runtime rather than in the console's buffer.

        Guarding this read with ``kbhit`` therefore loses every arrow
        key, and strands its scan code to be read back later as a
        keystroke of its own -- so the next key pressed is misread as
        well.

        Returns:
            The second character. The read is unguarded, which is safe
            precisely because a prefix means the runtime is holding it:
            it returned in under a millisecond when measured.
        """
        import msvcrt

        return msvcrt.getwch()

    def _is_console() -> bool:
        """Whether standard input really is a console.

        ``isatty`` is not enough here, and the difference is a hang rather
        than a wrong answer. Windows calls every character device a terminal,
        so redirecting from ``NUL`` reports a terminal, and then nothing is
        ever typed at it. Asking the console API settles it: the call fails
        for anything that is not a console.

        Returns:
            True if keystrokes can arrive.
        """
        import ctypes

        # ctypes rather than pywin32 throughout: importing pythoncom
        # initialises COM as an apartment and makes bleak hang.
        stdin_handle = -10  # STD_INPUT_HANDLE
        handle = ctypes.windll.kernel32.GetStdHandle(stdin_handle)
        mode = ctypes.c_uint()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))

else:

    @contextlib.contextmanager
    def reading_keys() -> Generator[None]:
        """Put the terminal in cbreak mode for the duration of the block.

        Cbreak rather than raw, so that the kernel still turns Ctrl-C into a
        signal. That way the way out of a chooser does not depend on this
        module reading and interpreting a byte correctly.

        Yields:
            Nothing. The previous settings are restored on the way out, by a
            synchronous ``__exit__`` that cannot be cancelled.
        """
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)

        try:
            tty.setcbreak(fd)
            yield
        finally:
            # TCSADRAIN so that anything already written is not cut off.
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def read_second() -> str | None:
        """Read the next character of a sequence, if one is waiting.

        The counterpart of the Windows version, and here it is simply
        another read: every byte of an escape sequence is in the
        terminal's own buffer, so asking whether one is waiting is the
        right question.

        Returns:
            The character, or ``None`` if nothing more has arrived.
        """
        return read_ready()

    def read_ready() -> str | None:
        """Read one character if the terminal has one waiting.

        Returns:
            The character, or ``None`` if nothing has been typed.
        """
        import select

        readable, _, _ = select.select([sys.stdin], [], [], 0)
        return sys.stdin.read(1) if readable else None

    def _is_console() -> bool:
        """Whether standard input really is a terminal.

        On POSIX ``isatty`` means what it says, so there is nothing more to
        ask. The Windows implementation has to work harder.

        Returns:
            True, the caller having already checked ``isatty``.
        """
        return True
