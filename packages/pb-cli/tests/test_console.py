# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The terminal in raw mode, against a real one.

Nothing here is simulated, because the things worth testing are exactly the
ones a simulation would assume: that the console really does hand back escape
sequences for the arrow keys, that Ctrl-C really does arrive as a byte instead
of killing the process, and that the terminal really is put back the way it was
found. A fake console would answer all three by construction.

A real terminal is arranged differently on each platform. On Windows the probe
allocates a console of its own and types into it with ``WriteConsoleInputW``,
which goes through the same input buffer a keyboard does. On POSIX the test
opens a pseudo-terminal, hands it to the probe as its standard input and
output, and types into the other end. Either way the probe reports what it saw
as JSON, so the assertions are about observed behaviour rather than about which
functions were called.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

TYPED = "ab\x1b[A\x03"
"""What the test types: two letters, an up arrow, and Ctrl-C.

The arrow is the one that proves the platforms agree -- both must deliver the
three bytes of the escape sequence rather than a key code. Ctrl-C is the one
that proves the process is not about to be interrupted by its own input.
"""

_PROBE = """
import json
import sys
import time

import anyio

from pb_cli import console

REPORT = sys.argv[1]
WANT = {want!r}
report = {{}}

{arrange}


async def collect(term, until):
    "Read until the wanted bytes have arrived, waiting rather than polling."
    got = b""
    with anyio.fail_after(20):
        while len(got) < until:
            chunk = term.read()
            if chunk:
                got += chunk
                continue
            report["waits"] = report.get("waits", 0) + 1
            await term.wait()
    return got


async def held_off(term, seconds):
    "How long a wait lasts when there is nothing to read, and what turned up."
    started = time.perf_counter()
    with anyio.move_on_after(seconds):
        await term.wait()
    waited = time.perf_counter() - started
    # Whatever arrived, if anything did. A wait that ends early ended because
    # the terminal had something, and saying what it was turns a puzzling
    # failure into a report.
    return [waited, term.read().decode("utf-8", "replace")]


async def main():
    with console.raw_console() as term:
        report["mode_raw"] = _mode()
        term.write(b"written by the probe")
        report["size"] = list(term.size())
        {type_keys}
        report["read"] = (await collect(term, len(WANT.encode()))).decode(
            "utf-8", "replace"
        )

        # A wait with nothing to read has to be released by cancellation
        # rather than holding on until somebody presses a key.
        report["cancelled_wait_s"] = await held_off(term, 0.3)
        # And the wait after it has to be a fresh one. If releasing the last
        # one left anything set, this returns at once instead of waiting.
        report["next_wait_s"] = await held_off(term, 0.3)

        if sys.platform == "win32":
            # A record that carries no character still signals the console.
            # Reading has to consume it, or every wait from here on returns
            # immediately and the session spins on a full core.
            _focus_event()
            report["quiet_record_read"] = term.read().decode("utf-8", "replace")
            report["after_quiet_record_s"] = (await held_off(term, 0.3))[0]

    report["mode_after"] = _mode()


report["unavailable"] = console.why_unavailable()
report["mode_before"] = _mode()
anyio.run(main)
Path = __import__("pathlib").Path
Path(REPORT).write_text(json.dumps(report), encoding="utf-8")
"""

_WINDOWS_ARRANGE = """
import ctypes
import ctypes.wintypes as w

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _Char(ctypes.Union):
    _fields_ = [("UnicodeChar", w.WCHAR), ("AsciiChar", ctypes.c_char)]


class _Key(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", w.BOOL),
        ("wRepeatCount", w.WORD),
        ("wVirtualKeyCode", w.WORD),
        ("wVirtualScanCode", w.WORD),
        ("uChar", _Char),
        ("dwControlKeyState", w.DWORD),
    ]


# No padding member: the union is exactly as wide as its largest event, and
# padding it out would make every record after the first land at the wrong
# offset -- which reads as "only the first key arrived".
class _Event(ctypes.Union):
    _fields_ = [("KeyEvent", _Key)]


class _Record(ctypes.Structure):
    _fields_ = [("EventType", w.WORD), ("Event", _Event)]


# A console of this process's own, so the test needs no terminal of its own
# and cannot disturb whichever one it was started from.
k32.FreeConsole()
if not k32.AllocConsole():
    raise OSError(ctypes.get_last_error(), "no console")


def _conin():
    handle = k32.CreateFileW(
        "CONIN$", 0x80000000 | 0x40000000, 1 | 2, None, 3, 0, None
    )
    if handle == -1:
        raise OSError(ctypes.get_last_error(), "cannot open CONIN$")
    return handle


# Point this process's standard handles at the console just allocated. A
# process started with its output captured has pipes for standard handles, and
# allocating a console does not replace them, so without this the probe would
# look exactly like the case it is meant to reject.
def _adopt():
    conin = _conin()
    conout = k32.CreateFileW(
        "CONOUT$", 0x80000000 | 0x40000000, 1 | 2, None, 3, 0, None
    )
    k32.SetStdHandle(-10, conin)
    k32.SetStdHandle(-11, conout)
    sys.stdin = open("CONIN$", "r", encoding="utf-8")
    sys.stdout = open("CONOUT$", "w", encoding="utf-8")


def _mode():
    handle = _conin()
    mode = w.DWORD()
    k32.GetConsoleMode(handle, ctypes.byref(mode))
    k32.CloseHandle(handle)
    return mode.value


def _type(text):
    handle = _conin()
    records = (_Record * len(text))()
    for index, char in enumerate(text):
        records[index].EventType = 1
        records[index].Event.KeyEvent.bKeyDown = True
        records[index].Event.KeyEvent.wRepeatCount = 1
        records[index].Event.KeyEvent.uChar.UnicodeChar = char
    written = w.DWORD()
    k32.WriteConsoleInputW(handle, records, len(text), ctypes.byref(written))
    k32.CloseHandle(handle)


def _type_later(delay, text):
    import threading

    threading.Timer(delay, _type, (text,)).start()


def _focus_event():
    # The console window gaining or losing focus. It is a record like any
    # other, so it signals the handle, and it carries no character -- which
    # makes it exactly what a reader that does not drain its records spins on.
    # A key going *up* would not do: with virtual terminal input the console
    # discards those before they are ever queued.
    handle = _conin()
    records = (_Record * 1)()
    records[0].EventType = 0x0010
    written = w.DWORD()
    k32.WriteConsoleInputW(handle, records, 1, ctypes.byref(written))
    k32.CloseHandle(handle)


_adopt()

# A console does not necessarily start empty -- measured as three backspaces
# one time and two letters the next -- and anything left in it would be read
# as though the test had typed it.
k32.FlushConsoleInputBuffer(_conin())
"""

_POSIX_ARRANGE = """
import termios


def _mode():
    flags = termios.tcgetattr(sys.stdin.fileno())
    return {"echo": bool(flags[3] & termios.ECHO), "isig": bool(flags[3] & termios.ISIG)}
"""


def _probe(tmp_path: Path, arrange: str, type_keys: str) -> Path:
    """Write the probe program that will run against a real terminal."""
    source = _PROBE.format(
        want=TYPED, arrange=textwrap.dedent(arrange), type_keys=type_keys
    )
    path = tmp_path / "console_probe.py"
    path.write_text(source, encoding="utf-8")
    return path


def _report(path: Path) -> dict[str, object]:
    assert path.exists(), "the probe wrote no report"
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_waiting_works(report: dict[str, object]) -> None:
    """Check the properties waiting has to have on either platform.

    Args:
        report: What the probe observed.
    """
    # Nothing was there to read, so the keystrokes were waited for rather than
    # found by looking again and again.
    assert int(str(report["waits"])) >= 1, "the probe never had to wait"

    # A wait with nothing to read ends when the caller stops waiting. If it
    # held on until a key was pressed, a session could not be left.
    for when in ("cancelled_wait_s", "next_wait_s"):
        held, arrived = cast("Sequence[Any]", report[when])
        assert not arrived, f"{when} ended early because {arrived!r} arrived"
        assert 0.2 <= float(held) < 5.0, f"{when} was {held}s, wanted about 0.3s"


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows console")
def test_a_real_windows_console_gives_back_what_was_typed(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    # Typed from a timer rather than up front, so the probe finds nothing to
    # read and has to wait for it -- which is the path under test.
    probe = _probe(tmp_path, _WINDOWS_ARRANGE, "_type_later(0.3, WANT)")

    result = subprocess.run(
        [sys.executable, str(probe), str(report_path)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    report = _report(report_path)

    assert report["unavailable"] is None
    # The arrow key arrives as the escape sequence a terminal would send, not
    # as a key code, which is what lets everything above this be shared.
    assert report["read"] == TYPED
    # Ctrl-C came back as a byte rather than interrupting the probe, which is
    # what makes it possible to send one to the hub.
    assert "\x03" in str(report["read"])

    raw = int(str(report["mode_raw"]))
    assert raw & 0x0200, "virtual terminal input was not enabled"
    assert not raw & 0x0001, "processed input would have swallowed Ctrl-C"
    assert not raw & 0x0004, "echo would have doubled everything typed"
    # And the console is left exactly as it was found.
    assert report["mode_after"] == report["mode_before"]
    _assert_waiting_works(report)

    # The regression this design exists to avoid. A focus event wakes the wait
    # and carries no character, so unless the read consumes it, every wait
    # after it returns at once and the session spins on a full core.
    assert report["quiet_record_read"] == ""
    assert float(str(report["after_quiet_record_s"])) >= 0.25, (
        "a record with no character was left in the buffer, so waiting no longer waits"
    )


def _wait_for(fd: int, marker: bytes, seconds: float = 30.0) -> None:
    """Read from a terminal until it says what we are waiting for.

    Args:
        fd: The controlling end of the pseudo-terminal.
        marker: What the other end will write when it is ready.
        seconds: How long to wait before giving up.

    Raises:
        AssertionError: If the marker never arrived.
    """
    import os
    import select
    import time

    seen = b""
    deadline = time.monotonic() + seconds

    while marker not in seen and time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.1)
        if readable:
            seen += os.read(fd, 4096)

    assert marker in seen, f"the probe never said it was ready: {seen!r}"


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX pseudo-terminal")
def test_a_real_pty_gives_back_what_was_typed(tmp_path: Path) -> None:
    import os
    import pty

    report_path = tmp_path / "report.json"
    probe = _probe(tmp_path, _POSIX_ARRANGE, "")

    controller, follower = pty.openpty()

    try:
        with subprocess.Popen(
            [sys.executable, str(probe), str(report_path)],
            stdin=follower,
            stdout=follower,
            stderr=subprocess.PIPE,
        ) as process:
            os.close(follower)
            # Not a moment sooner: until the probe has entered raw mode the
            # line discipline is still buffering whole lines and turning
            # Ctrl-C into a signal, so anything typed now would be answered by
            # the terminal rather than by the code under test. The probe
            # writing to the terminal is what says it is ready.
            _wait_for(controller, b"written by the probe")
            os.write(controller, TYPED.encode())
            _stdout, stderr = process.communicate(timeout=60)
            assert process.returncode == 0, stderr.decode()
    finally:
        os.close(controller)

    report = _report(report_path)
    assert report["unavailable"] is None
    assert report["read"] == TYPED
    # Raw mode, so the terminal echoes nothing and turns nothing into a signal.
    assert report["mode_raw"] == {"echo": False, "isig": False}
    # And it is left exactly as it was found.
    assert report["mode_after"] == report["mode_before"]
    _assert_waiting_works(report)
