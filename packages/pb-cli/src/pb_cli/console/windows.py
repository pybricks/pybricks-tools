# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The Windows console, in raw mode.

Waiting is the hard half on this platform, and the shape here is the one
prompt_toolkit arrived at independently: **wait in a thread, read on the loop.**

No event loop will wait on a console handle. A console is not a socket, so
neither ``anyio.wait_readable`` nor the proactor loop's public surface will take
one, and the proactor cannot be swapped for a selector loop because the
service's named pipes need it. So a thread does the waiting, in
``WaitForMultipleObjects`` over the console handle and an event of ours -- and
setting that event releases it in microseconds, which is what keeps the thread
inside the task tree rather than abandoned outside it.

**The thread waits; it never reads.** That distinction is the whole design.
A thread parked in ``ReadConsoleInputW`` cannot be released without breaking the
console: measured on Windows 11, ``CancelSynchronousIo`` and ``CancelIoEx`` both
abort the read, and afterwards *every* read on the console fails to return for
the life of the process -- reopening ``CONIN$`` does not recover it. An
abandoned reader is no better, because it goes on eating keystrokes after cooked
mode is restored, so the next command typed at the shell loses its first
characters. A thread parked in a wait has neither problem: it consumes nothing
and it lets go the instant it is asked.

**Records that produce no characters are drained rather than ignored.** A
console reports more than typing -- the window gaining or losing focus is a
record like any other -- and every record signals the handle whether or not it
carries a character. So the read takes every record waiting and keeps only the
characters. That is not tidiness: a record left in the buffer keeps the handle
signalled, so every wait after it returns at once and the session spins on a
full core. Measured: with a focus event waiting, a zero-length wait on the
handle reports it signalled; with the buffer drained, it does not.

``msvcrt`` cannot do the draining, which is why this reads records itself.
``kbhit`` reports a focus event as nothing waiting -- true, there is no
character -- and leaves it exactly where it was. Measured: one focus event
written, ``kbhit`` false, and one record still in the buffer afterwards.

Keys going back up are *not* part of this, which is worth saying because they
look like the obvious culprit and are the first thing tried. With
``ENABLE_VIRTUAL_TERMINAL_INPUT`` set the console does not queue them at all:
written into the buffer, a key-up record is discarded before anything can read
it. Measured, after assuming otherwise.

What is *not* needed here, because ``ENABLE_VIRTUAL_TERMINAL_INPUT`` is set, is
the translation from virtual key codes to escape sequences that other tools
carry a few hundred lines for. The console has already done it, so a record's
character is a byte of the sequence a terminal would have sent.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes as wintypes
import shutil
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread

if TYPE_CHECKING:
    from pb_cli.console import Console

__all__ = ["platform_problem", "raw_console"]

READ_LIMIT = 4096
"""Characters to take from the console in one go.

Well past what typing produces and enough that pasting a block of code arrives
in a couple of reads rather than a hundred.
"""

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value

_STD_INPUT = -10
_STD_OUTPUT = -11

_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_EXTENDED_FLAGS = 0x0080
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

_ENABLE_PROCESSED_OUTPUT = 0x0001
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
_ENABLE_WRAP_AT_EOL_OUTPUT = 0x0002
_DISABLE_NEWLINE_AUTO_RETURN = 0x0008

_CP_UTF8 = 65001
_INFINITE = 0xFFFFFFFF
_KEY_EVENT = 0x0001

_RAW_INPUT_MODE = _ENABLE_VIRTUAL_TERMINAL_INPUT | _ENABLE_EXTENDED_FLAGS
"""What the console input has to be for a session with a hub.

Everything else is cleared, and each omission matters. Without
``ENABLE_LINE_INPUT`` and ``ENABLE_ECHO_INPUT`` there is no line buffering and
no local echo, which is what lets the hub do its own echoing and line editing.
Without ``ENABLE_PROCESSED_INPUT`` Ctrl-C arrives as the byte ``0x03`` and
raises no console control event, which is the whole reason an interrupt can be
sent to the interpreter. ``ENABLE_MOUSE_INPUT`` and ``ENABLE_WINDOW_INPUT`` are
off because they queue records that produce no characters.
``ENABLE_QUICK_EDIT_MODE`` is off because a stray mouse drag would otherwise
freeze the session -- and clearing it only takes effect when
``ENABLE_EXTENDED_FLAGS`` is set in the same call, which is why that flag is
here.
"""


class _Char(ctypes.Union):
    _fields_ = (("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char))


class _Key(ctypes.Structure):
    _fields_ = (
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", _Char),
        ("dwControlKeyState", wintypes.DWORD),
    )


class _Event(ctypes.Union):
    _fields_ = (("KeyEvent", _Key),)


class _Record(ctypes.Structure):
    """One console input record.

    The union carries only the key event, which is what makes this the right
    width: every other event this console can produce is smaller, so a record
    is as wide as the real one and the records after the first land where they
    should. Getting that wrong reads as "only the first key arrived".
    """

    _fields_ = (("EventType", wintypes.WORD), ("Event", _Event))


# ctypes rather than pywin32 throughout: importing pythoncom initialises COM as
# an apartment, which makes bleak hang.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _entry(name: str, restype: Any, *argtypes: Any) -> Any:
    """Bind one kernel32 function, with a signature of our own.

    A prototype rather than ``getattr`` and then ``argtypes``, because the
    function object a library hands out is shared with every other caller in
    the process: declaring types on it would change how somebody else's call is
    marshalled. This returns a function of our own and leaves theirs alone.

    Declaring the types at all matters because ctypes otherwise treats
    everything as a C ``int``, and a handle passed as four bytes works only for
    as long as the values stay small.

    Args:
        name: The exported symbol.
        restype: What it returns.
        *argtypes: What it takes.

    Returns:
        A callable bound to that symbol.
    """
    prototype = ctypes.WINFUNCTYPE(restype, *argtypes, use_last_error=True)

    return prototype((name, _kernel32))


_CreateFileW = _entry(
    "CreateFileW",
    wintypes.HANDLE,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
)
_CreateEventW = _entry(
    "CreateEventW",
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.BOOL,
    wintypes.LPCWSTR,
)
_CloseHandle = _entry("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_SetEvent = _entry("SetEvent", wintypes.BOOL, wintypes.HANDLE)
_ResetEvent = _entry("ResetEvent", wintypes.BOOL, wintypes.HANDLE)
_WaitForMultipleObjects = _entry(
    "WaitForMultipleObjects",
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.BOOL,
    wintypes.DWORD,
)
_GetStdHandle = _entry("GetStdHandle", wintypes.HANDLE, wintypes.DWORD)
_GetConsoleMode = _entry(
    "GetConsoleMode", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
)
_SetConsoleMode = _entry(
    "SetConsoleMode", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD
)
_GetConsoleOutputCP = _entry("GetConsoleOutputCP", wintypes.UINT)
_SetConsoleOutputCP = _entry("SetConsoleOutputCP", wintypes.BOOL, wintypes.UINT)
_GetNumberOfConsoleInputEvents = _entry(
    "GetNumberOfConsoleInputEvents",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.DWORD),
)
_ReadConsoleInputW = _entry(
    "ReadConsoleInputW",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
)
_WriteFile = _entry(
    "WriteFile",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
)


def platform_problem() -> str | None:
    """Whether the standard handles really belong to a console.

    ``isatty`` is not enough on Windows: it is true for every character device,
    so a redirect from ``NUL`` claims to be a terminal. Git Bash's ``mintty``
    is the other case worth naming, and the common one on this platform -- it
    is a pty emulated over pipes, so a native Python sees no console at all.

    Returns:
        A sentence naming the problem, or None.
    """
    mode = wintypes.DWORD()

    for name, std in (("input", _STD_INPUT), ("output", _STD_OUTPUT)):
        handle = _GetStdHandle(std)
        if not _GetConsoleMode(handle, ctypes.byref(mode)):
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

        Raises:
            OSError: If the wake-up event cannot be created.
        """
        self._conin = conin
        self._conout = conout
        self._pending = ""
        # Manual reset, so that setting it releases a thread that has not
        # reached the wait yet as surely as one already in it. Every wait
        # clears it first, so a set from a previous wait cannot leak into the
        # next one.
        wake = _CreateEventW(None, True, False, None)
        if not wake:
            raise OSError(ctypes.get_last_error(), "cannot create a wake-up event")

        self._wake = wake
        self._waited = (wintypes.HANDLE * 2)(wake, conin)

    async def wait(self) -> None:
        """Wait until the console has input, or this task is cancelled.

        The thread is inside the task tree rather than beside it: whichever way
        this returns, it does not return until the thread has. That holds on
        cancellation too, because what unwinds the cancellation is the same
        event that releases the thread.
        """
        _ResetEvent(self._wake)

        async with anyio.create_task_group() as tg:

            async def watch() -> None:
                # abandon_on_cancel=False: a cancel waits for the thread, which
                # is safe precisely because the thread is about to be released
                # by the event set below.
                await anyio.to_thread.run_sync(self._block, abandon_on_cancel=False)
                tg.cancel_scope.cancel()

            tg.start_soon(watch)

            try:
                await anyio.sleep_forever()

            finally:
                # Either input arrived and the watcher cancelled us, or the
                # caller did. Both mean the thread has to be let go before this
                # group can exit. A synchronous call, so a cancelled scope
                # cannot skip it.
                _SetEvent(self._wake)

    def _block(self) -> None:
        """Block until the console has input or the wake-up event is set.

        Runs in a worker thread. ctypes releases the interpreter lock around
        the call, so nothing else is held up by it.
        """
        _WaitForMultipleObjects(2, self._waited, False, _INFINITE)

    def read(self) -> bytes:
        """Take whatever has been typed, as UTF-8.

        Every record waiting is taken, and the ones that carry a character are
        kept. In virtual terminal mode those characters are already the escape
        sequences a terminal would have sent. They are UTF-16 code units, so a
        character outside the basic plane arrives as two of them and the first
        is held back until its other half comes.

        Returns:
            The bytes typed, or empty.
        """
        text = self._pending
        self._pending = ""
        text += self._take()
        if text and "\ud800" <= text[-1] <= "\udbff":
            # A high surrogate with its partner still to come.
            self._pending = text[-1]
            text = text[:-1]

        return text.encode("utf-8", "surrogatepass")

    def _take(self) -> str:
        """Drain the input buffer, keeping only what is a character.

        Returns:
            What was typed, as text.
        """
        typed: list[str] = []

        while len(typed) < READ_LIMIT:
            waiting = wintypes.DWORD()
            if not _GetNumberOfConsoleInputEvents(self._conin, ctypes.byref(waiting)):
                break

            count = min(waiting.value, READ_LIMIT)
            if not count:
                break

            records = (_Record * count)()
            taken = wintypes.DWORD()
            if not _ReadConsoleInputW(self._conin, records, count, ctypes.byref(taken)):
                break

            for index in range(taken.value):
                record = records[index]
                if record.EventType != _KEY_EVENT:
                    continue

                key = record.Event.KeyEvent
                if not key.bKeyDown:
                    continue

                char = key.uChar.UnicodeChar
                if not char or char == "\x00":
                    continue

                typed.append(char * max(1, key.wRepeatCount))

        return "".join(typed)

    def write(self, data: bytes) -> None:
        """Write bytes to the console, byte for byte.

        The console's output code page is UTF-8 for the duration of the
        session, so the hub's bytes go out untouched. Decoding them here would
        corrupt any that are not valid UTF-8, and hub output is arbitrary
        bytes.

        Args:
            data: What to write.
        """
        written = wintypes.DWORD()
        _WriteFile(self._conout, data, len(data), ctypes.byref(written), None)

    def size(self) -> tuple[int, int]:
        """Return the console's size.

        Returns:
            Columns and rows.
        """
        columns, lines = shutil.get_terminal_size()

        return columns, lines

    def close(self) -> None:
        """Release anything still waiting, and let the event go."""
        _SetEvent(self._wake)
        _CloseHandle(self._wake)


def _open(name: str) -> int:
    """Open one of the console's own devices.

    ``CONIN$`` and ``CONOUT$`` rather than the standard handles, so this always
    talks to the real console even where stdio has been redirected, and so
    restoring can never target the wrong object.

    Args:
        name: ``CONIN$`` or ``CONOUT$``.

    Returns:
        The handle.

    Raises:
        OSError: If the console cannot be opened.
    """
    handle = _CreateFileW(
        name,
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    if handle in (None, _INVALID_HANDLE):
        raise OSError(ctypes.get_last_error(), f"cannot open {name}")

    return handle


@contextlib.contextmanager
def raw_console() -> Generator[Console]:
    """Put the console into raw mode for the duration of the block.

    Yields:
        The console.

    Raises:
        OSError: If the console cannot be opened.
    """
    conin = _open("CONIN$")
    conout = _open("CONOUT$")

    saved_in = wintypes.DWORD()
    saved_out = wintypes.DWORD()
    _GetConsoleMode(conin, ctypes.byref(saved_in))
    _GetConsoleMode(conout, ctypes.byref(saved_out))
    saved_cp = _GetConsoleOutputCP()

    _SetConsoleMode(conin, _RAW_INPUT_MODE)
    _SetConsoleMode(
        conout,
        saved_out.value
        | _ENABLE_PROCESSED_OUTPUT
        | _ENABLE_WRAP_AT_EOL_OUTPUT
        | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
        | _DISABLE_NEWLINE_AUTO_RETURN,
    )
    _SetConsoleOutputCP(_CP_UTF8)
    console = _WindowsConsole(conin, conout)

    try:
        yield console

    finally:
        # Synchronous, so no cancellation can skip it. The console is released
        # before its handles are closed, so nothing is left waiting on one that
        # has gone. The output code page is process-wide console state and is
        # restored with the modes.
        console.close()
        _SetConsoleOutputCP(saved_cp)
        _SetConsoleMode(conin, saved_in)
        _SetConsoleMode(conout, saved_out)
        _CloseHandle(conin)
        _CloseHandle(conout)
