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
from pathlib import Path

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

from pb import console

REPORT = sys.argv[1]
WANT = {want!r}
report = {{}}

{arrange}

report["unavailable"] = console.why_unavailable()
report["mode_before"] = _mode()

with console.raw_console() as term:
    report["mode_raw"] = _mode()
    term.write(b"written by the probe")
    report["size"] = list(term.size())
    {type_keys}
    got = b""
    deadline = time.monotonic() + 10
    while len(got) < len(WANT.encode()) and time.monotonic() < deadline:
        got += term.read()
        time.sleep(0.01)
    report["read"] = got.decode("utf-8", "replace")

report["mode_after"] = _mode()
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


_adopt()
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


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows console")
def test_a_real_windows_console_gives_back_what_was_typed(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    probe = _probe(tmp_path, _WINDOWS_ARRANGE, "_type(WANT)")

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
