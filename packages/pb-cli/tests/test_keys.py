# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Turning what a terminal sends into what a key means.

Reading is per platform; deciding is not, and deciding is where the traps are.
Most of these drive the decision directly with a scripted stream of
characters, so the Windows grammar and the terminal grammar are both covered
wherever the tests happen to run.

One is not scripted, and has to be. A scripted stream assumes both halves of
a special key can be read the same way, and on Windows they cannot -- which
is a bug the scripted tests passed straight through. So the last test here
types a real arrow key into a real console and asks what came out.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from pb_cli import keys
from pb_cli.keys import Key


def _typed(monkeypatch: pytest.MonkeyPatch, *chars: str) -> None:
    """Queue up characters for the next reads, then nothing.

    Both reads draw on the one queue, because a scripted stream is standing
    in for a keyboard rather than for either platform's way of reaching one.
    """
    waiting: Iterator[str] = iter(chars)

    def _next() -> str | None:
        return next(waiting, None)

    monkeypatch.setattr(keys, "read_ready", _next)
    monkeypatch.setattr(keys, "read_second", _next)


def test_nothing_typed_is_not_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The chooser polls constantly, so this is the answer nearly every time.
    _typed(monkeypatch)
    assert keys.poll_key() is None


@pytest.mark.parametrize("char", ["\r", "\n"])
def test_enter_arrives_either_way(monkeypatch: pytest.MonkeyPatch, char: str) -> None:
    _typed(monkeypatch, char)
    assert keys.poll_key() is Key.ENTER


@pytest.mark.parametrize("prefix", ["\x00", "\xe0"])
def test_the_windows_arrows(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    # The console sends a prefix and then a scan code, and uses either prefix
    # depending on the key.
    _typed(monkeypatch, prefix, "H")
    assert keys.poll_key() is Key.UP
    _typed(monkeypatch, prefix, "P")
    assert keys.poll_key() is Key.DOWN


def test_the_terminal_arrows(monkeypatch: pytest.MonkeyPatch) -> None:
    _typed(monkeypatch, "\x1b", "[", "A")
    assert keys.poll_key() is Key.UP
    _typed(monkeypatch, "\x1b", "[", "B")
    assert keys.poll_key() is Key.DOWN


def test_escape_on_its_own_means_leave(monkeypatch: pytest.MonkeyPatch) -> None:
    # The trap: escape both stands alone and opens the arrow sequences. What
    # tells them apart is that nothing follows a bare escape.
    _typed(monkeypatch, "\x1b")
    assert keys.poll_key() is Key.CANCEL


def test_an_unknown_escape_sequence_is_not_mistaken_for_an_arrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Page Up, a function key, a mouse report: plenty arrives that this does
    # not know, and none of it should move the selection.
    _typed(monkeypatch, "\x1b", "[", "5", "~")
    assert keys.poll_key() is Key.OTHER


def test_an_unknown_windows_scan_code_is_ignored_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _typed(monkeypatch, "\x00", "K")
    assert keys.poll_key() is Key.OTHER


@pytest.mark.parametrize("char", ["q", "Q"])
def test_q_leaves(monkeypatch: pytest.MonkeyPatch, char: str) -> None:
    _typed(monkeypatch, char)
    assert keys.poll_key() is Key.CANCEL


def test_an_ordinary_letter_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _typed(monkeypatch, "x")
    assert keys.poll_key() is Key.OTHER


def test_keys_cannot_be_read_when_input_is_not_a_terminal() -> None:
    # pytest captures stdin, so this is the piped case. It is the guard that
    # stops a chooser waiting forever for a keystroke that cannot come.
    assert keys.keys_available() is False


_CONSOLE_PROBE = """
import ctypes
import ctypes.wintypes as w
import json
import sys
import time
from pathlib import Path

from pb_cli import keys

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


# No padding member: the union is exactly as wide as its largest event.
class _Event(ctypes.Union):
    _fields_ = [("KeyEvent", _Key)]


class _Record(ctypes.Structure):
    _fields_ = [("EventType", w.WORD), ("Event", _Event)]


conin = k32.CreateFileW("CONIN$", 0x80000000 | 0x40000000, 1 | 2, None, 3, 0, None)
if conin == -1:
    raise OSError(ctypes.get_last_error(), "cannot open CONIN$")


def _press(code, scan, char):
    records = (_Record * 2)()
    for half, down in ((0, 1), (1, 0)):
        record = records[half]
        record.EventType = 1
        record.Event.KeyEvent.bKeyDown = down
        record.Event.KeyEvent.wRepeatCount = 1
        record.Event.KeyEvent.wVirtualKeyCode = code
        record.Event.KeyEvent.wVirtualScanCode = scan
        record.Event.KeyEvent.uChar.UnicodeChar = char
    written = w.DWORD()
    k32.WriteConsoleInputW(conin, records, 2, ctypes.byref(written))


import msvcrt

# A console does not necessarily start empty -- measured, and what was in it
# was three backspaces one time and two letters the next.
time.sleep(0.5)
while msvcrt.kbhit():
    msvcrt.getwch()

# Down, up, enter: the three a chooser lives on. The arrows carry no
# character of their own, which is the whole point -- what identifies them is
# the scan code, and it arrives separately.
#
# One key at a time, waiting for each. Queuing all three would pass whether
# this works or not, because the halves of a special key are only split when
# nothing else is waiting behind them -- and one key at a time is how a
# person types.
seen = []
for code, scan, char in ((0x28, 0x50, chr(0)), (0x26, 0x48, chr(0)), (0x0D, 0x1C, chr(13))):
    _press(code, scan, char)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        key = keys.poll_key()
        if key is not None:
            seen.append(key.name)
            break
        time.sleep(0.01)
    else:
        seen.append("nothing")

Path(sys.argv[1]).write_text(json.dumps(seen), encoding="utf-8")
"""


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows console")
def test_a_real_arrow_key_in_a_real_console(tmp_path: Path) -> None:
    # The one that catches what a scripted stream cannot. A special key
    # reaches a program in two halves, and the runtime hands over the first
    # and keeps the second -- so kbhit says nothing is waiting, and a read
    # guarded by it loses every arrow key and strands the scan code to be
    # misread as the next keystroke. Both halves have to come out as one key.
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent(_CONSOLE_PROBE), encoding="utf-8")
    report = tmp_path / "keys.json"

    # A console of its own, from the start rather than allocated afterwards:
    # the runtime keeps the console it was given when the process began, so
    # keys typed into one allocated later never reach it.
    create_new_console = 0x00000010
    result = subprocess.run(
        [sys.executable, str(probe), str(report)],
        capture_output=True,
        timeout=60,
        check=False,
        creationflags=create_new_console,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert report.exists(), "the probe wrote no report"
    assert json.loads(report.read_text(encoding="utf-8")) == ["DOWN", "UP", "ENTER"]
