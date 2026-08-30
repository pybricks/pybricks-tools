# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The terminal, in raw mode, as bytes in and bytes out.

This is the piece :mod:`pb.keys` deliberately is not. A chooser only has to
tell up from down from Enter; a session with a hub has to deliver *every* byte
a key produces, unchanged, including the ones that would otherwise be a signal
-- Ctrl-C is a byte for the interpreter on the hub, not an interrupt for us.

Three properties are what make this tractable, and each one removes a class of
bug that hand-rolled terminal code is famous for.

**Reading never blocks.** ``read`` returns whatever is waiting and nothing else,
so the caller stays in its own event loop. There is no reader thread to abandon,
no uninterruptible read to cancel, and no wake-up event to get wrong. That is
worth more than the few milliseconds a poll costs, and a poll interval far below
human typing speed is not perceptible.

**Windows and POSIX produce the same bytes.** Setting
``ENABLE_VIRTUAL_TERMINAL_INPUT`` makes the console encode special keys as the
escape sequences a terminal would send, so an arrow key really does arrive as
``ESC [ A`` on both. Measured, not assumed: with that flag set, ``msvcrt`` hands
back the three characters of the sequence one at a time, exactly as a terminal
does. Everything above this module is therefore shared, and none of the
vkey-to-sequence translation tables that other tools carry is needed.

**Leaving raw mode is a synchronous ``__exit__``.** A sync exit cannot be
cancelled at all, which is stronger than shielding, so the terminal is restored
on every ordinary path -- return, exception, cancellation -- by the language
itself rather than by anything this module remembers to do.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from collections.abc import Generator
from typing import Protocol, runtime_checkable

__all__ = ["Console", "available", "raw_console", "why_unavailable"]

READ_LIMIT = 4096
"""Bytes to take from the terminal in one go.

Well past what typing produces and enough that pasting a block of code arrives
in a couple of reads rather than a hundred.
"""


@runtime_checkable
class Console(Protocol):
    """A terminal that has been put into raw mode."""

    def read(self) -> bytes:
        """Take whatever has been typed.

        Returns:
            The bytes waiting, or empty if nothing has been typed. Never
            blocks.
        """
        ...

    def write(self, data: bytes) -> None:
        """Put bytes on the screen exactly as given.

        Args:
            data: What to write. Not decoded, not translated: it may be part
                of an escape sequence, or half of a character whose other half
                has not arrived.
        """
        ...

    def size(self) -> tuple[int, int]:
        """Return the terminal's size.

        Returns:
            Columns and rows.
        """
        ...


def why_unavailable() -> str | None:
    """Explain why a raw terminal cannot be had here.

    Returns:
        A sentence naming the problem, or ``None`` if there is no problem.
    """
    for name, stream in (("input", sys.stdin), ("output", sys.stdout)):
        try:
            if not stream.isatty():
                return f"standard {name} is not a terminal"
        except (AttributeError, ValueError):  # pragma: no cover - odd embeddings
            return f"there is no standard {name}"
    return _why_unavailable()


def available() -> bool:
    """Whether an interactive session is possible here.

    Returns:
        True if this process has a terminal it can put into raw mode.
    """
    return why_unavailable() is None


if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wintypes

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _INVALID_HANDLE = -1

    _ENABLE_PROCESSED_INPUT = 0x0001
    _ENABLE_EXTENDED_FLAGS = 0x0080
    _ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

    _ENABLE_PROCESSED_OUTPUT = 0x0001
    _ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    _ENABLE_WRAP_AT_EOL_OUTPUT = 0x0002
    _DISABLE_NEWLINE_AUTO_RETURN = 0x0008

    _CP_UTF8 = 65001

    _RAW_INPUT_MODE = _ENABLE_VIRTUAL_TERMINAL_INPUT | _ENABLE_EXTENDED_FLAGS
    """What the console input has to be for a session with a hub.

    Everything else is cleared, and each omission matters. Without
    ``ENABLE_LINE_INPUT`` and ``ENABLE_ECHO_INPUT`` there is no line buffering
    and no local echo, which is what lets the hub do its own echoing and line
    editing. Without ``ENABLE_PROCESSED_INPUT`` Ctrl-C arrives as the byte
    ``0x03`` and raises no console control event, which is the whole reason an
    interrupt can be sent to the interpreter. ``ENABLE_MOUSE_INPUT`` and
    ``ENABLE_WINDOW_INPUT`` are off because they queue records that produce no
    characters. ``ENABLE_QUICK_EDIT_MODE`` is off because a stray mouse drag
    would otherwise freeze the session -- and clearing it only takes effect
    when ``ENABLE_EXTENDED_FLAGS`` is set in the same call, which is why that
    flag is here.
    """

    def _kernel32() -> ctypes.WinDLL:
        # ctypes rather than pywin32 throughout: importing pythoncom
        # initialises COM as an apartment, which makes bleak hang.
        return ctypes.WinDLL("kernel32", use_last_error=True)

    def _why_unavailable() -> str | None:
        """Whether the standard handles really belong to a console.

        ``isatty`` is not enough on Windows: it is true for every character
        device, so a redirect from ``NUL`` claims to be a terminal. Git Bash's
        ``mintty`` is the other case worth naming, and the common one on this
        platform -- it is a pty emulated over pipes, so a native Python sees no
        console at all.

        Returns:
            A sentence naming the problem, or None.
        """
        k32 = _kernel32()
        mode = wintypes.DWORD()
        for name, std in (("input", -10), ("output", -11)):
            handle = k32.GetStdHandle(std)
            if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
                return (
                    f"standard {name} is not a Windows console (a terminal "
                    f"emulated over pipes, such as Git Bash, cannot do this)"
                )
        return None

    class _WindowsConsole:
        """The Windows console, in raw mode."""

        def __init__(self, conin: int, conout: int) -> None:
            """Hold the handles the modes were set on.

            Args:
                conin: The console input handle.
                conout: The console output handle.
            """
            self._conin = conin
            self._conout = conout
            self._k32 = _kernel32()
            self._pending = ""

        def read(self) -> bytes:
            """Take whatever has been typed, as UTF-8.

            ``msvcrt`` reads the console's own input buffer, which in virtual
            terminal mode already holds the escape sequences a terminal would
            have sent. What it hands back are UTF-16 code units, so a character
            outside the basic plane arrives as two of them and the first is
            held back until its other half comes.

            Returns:
                The bytes typed, or empty.
            """
            import msvcrt

            text = self._pending
            self._pending = ""
            while msvcrt.kbhit() and len(text) < READ_LIMIT:
                text += msvcrt.getwch()
            if text and "\ud800" <= text[-1] <= "\udbff":
                # A high surrogate with its partner still to come.
                self._pending = text[-1]
                text = text[:-1]
            return text.encode("utf-8", "surrogatepass")

        def write(self, data: bytes) -> None:
            """Write bytes to the console, byte for byte.

            The console's output code page is UTF-8 for the duration of the
            session, so the hub's bytes go out untouched. Decoding them here
            would corrupt any that are not valid UTF-8, and hub output is
            arbitrary bytes.

            Args:
                data: What to write.
            """
            written = wintypes.DWORD()
            self._k32.WriteFile(
                self._conout, data, len(data), ctypes.byref(written), None
            )

        def size(self) -> tuple[int, int]:
            """Return the console's size.

            Returns:
                Columns and rows.
            """
            columns, lines = shutil.get_terminal_size()
            return columns, lines

    @contextlib.contextmanager
    def raw_console() -> Generator[Console]:
        """Put the console into raw mode for the duration of the block.

        Yields:
            The console.

        Raises:
            OSError: If the console cannot be opened.
        """
        k32 = _kernel32()
        # CONIN$ and CONOUT$ rather than the standard handles, so this always
        # talks to the real console even where stdio has been redirected, and
        # so restoring can never target the wrong object.
        conin = k32.CreateFileW(
            "CONIN$",
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        conout = k32.CreateFileW(
            "CONOUT$",
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if conin == _INVALID_HANDLE or conout == _INVALID_HANDLE:
            raise OSError(ctypes.get_last_error(), "cannot open the console")

        saved_in = wintypes.DWORD()
        saved_out = wintypes.DWORD()
        k32.GetConsoleMode(conin, ctypes.byref(saved_in))
        k32.GetConsoleMode(conout, ctypes.byref(saved_out))
        saved_cp = k32.GetConsoleOutputCP()

        k32.SetConsoleMode(conin, _RAW_INPUT_MODE)
        k32.SetConsoleMode(
            conout,
            saved_out.value
            | _ENABLE_PROCESSED_OUTPUT
            | _ENABLE_WRAP_AT_EOL_OUTPUT
            | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
            | _DISABLE_NEWLINE_AUTO_RETURN,
        )
        k32.SetConsoleOutputCP(_CP_UTF8)
        try:
            yield _WindowsConsole(conin, conout)
        finally:
            # Synchronous, so no cancellation can skip it. The output code page
            # is process-wide console state and is restored with the modes.
            k32.SetConsoleOutputCP(saved_cp)
            k32.SetConsoleMode(conin, saved_in)
            k32.SetConsoleMode(conout, saved_out)
            k32.CloseHandle(conin)
            k32.CloseHandle(conout)

else:

    def _why_unavailable() -> str | None:
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

        Raw rather than cbreak, unlike :mod:`pb.keys`: the interpreter on the
        hub needs Ctrl-C as a byte, so the kernel must not turn it into a
        signal. Output processing goes too, which is why everything written
        while this is held ends its lines with a carriage return of its own.

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
