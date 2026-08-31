# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The status line along the bottom of an interactive session.

Pybricks Code keeps a bar at the foot of its window naming the hub and showing
its battery, and it is genuinely useful: what a user needs to know while typing
at a hub is which hub they are typing at, whether anything is listening, and
whether the battery is about to end the conversation. None of that belongs in
the scrolling text, where it would be pushed away by the next line of output.

**The line is pinned with a scrolling margin** -- ``DECSTBM``, which every
terminal in use here supports, conhost included. With the margin set to every
row but the last, the hub's output scrolls in the region above and the bottom
row is simply never touched by it. The hub's own line editing then behaves
exactly as it would with no status line at all, which matters more than it
sounds: the interpreter moves the cursor about as it echoes, and any scheme
that redrew the bar around that editing would fight it on every keystroke.

The alternate screen buffer was the other candidate and is what a full-screen
program would normally use. It is not used here because leaving it takes the
whole session with it, and the last thing printed at a REPL is usually the
thing you wanted to keep.

**Nothing here draws with rich.** In raw mode a bare newline does not return
the carriage, so every line this module writes ends with a carriage return of
its own, and a library that emits its own newlines would staircase down the
screen instead.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import pb_client
from pb_cli import escape
from pb_cli.console import Console

__all__ = ["NOTICE_SECONDS", "WARNINGS", "Banner", "describe_running"]

NOTICE_SECONDS: Final = 4.0
"""How long a passing message stays in the status line before it fades."""

WARNINGS: Final[Mapping[str, str]] = {
    "BATTERY_LOW_VOLTAGE_SHUTDOWN": "battery empty",
    "BATTERY_HIGH_TEMP_SHUTDOWN": "battery too hot",
    "BATTERY_LOW_VOLTAGE_WARNING": "battery low",
    "BATTERY_HIGH_TEMP_WARNING": "battery hot",
    "BATTERY_HIGH_CURRENT": "drawing too much",
    "BLE_LOW_SIGNAL": "weak signal",
    "SHUTDOWN_REQUEST": "switching off",
}
"""Status flags worth a word in the bar, worst first.

Only one is shown, because the bar has one line and the worst of them is the
one that matters. Ordinary states -- connected, advertising, a program running
-- are missing on purpose: they are either uninteresting or already said.

The battery is low or it is not: the hub reports two bits about it and no
level, so there is nothing here to draw a gauge from. See "Wanted from the
firmware" in the design notes.
"""

_SET_MARGIN = "\x1b[1;{bottom}r"
_RESET_MARGIN = "\x1b[r"
_SAVE_CURSOR = "\x1b7"
_RESTORE_CURSOR = "\x1b8"
_GOTO = "\x1b[{row};1H"
_CLEAR_LINE = "\x1b[2K"
_REVERSE = "\x1b[7m"
_NORMAL = "\x1b[0m"

_SEPARATOR = "  "


def describe_running(status: pb_client.Status | None) -> str:
    """Say what the hub is doing, in the words a user would use.

    Args:
        status: The hub's last report, or ``None`` if it has not made one.

    Returns:
        A short phrase for the status line.
    """
    if status is None:
        return "waiting for the hub"
    if status.repl_running:
        return "REPL"
    if status.program_running:
        return "program running"
    return "nothing running"


def worst_warning(status: pb_client.Status | None) -> str | None:
    """Return the most serious thing the hub is complaining about.

    Args:
        status: The hub's last report.

    Returns:
        The warning to show, or ``None`` if all is well.
    """
    if status is None:
        return None
    for flag, text in WARNINGS.items():
        if flag in status.names:
            return text
    return None


@dataclass
class _Layout:
    """What the status line says, before it is fitted to the screen."""

    left: str
    right: str

    def fit(self, columns: int) -> str:
        """Lay the two halves out across the given width.

        Args:
            columns: How wide the terminal is.

        Returns:
            One line of exactly ``columns - 1`` characters. One short of the
            width on purpose: writing the last cell of the last row makes some
            terminals scroll, which would take the status line with it.
        """
        width = max(1, columns - 1)
        right = self.right if len(self.left) + len(self.right) + 2 <= width else ""
        room = width - len(right)
        left = self.left[: max(0, room)]
        return f"{left}{' ' * (room - len(left))}{right}"


class Banner:
    """The status line, and the terminal region it lives outside of."""

    def __init__(self, console: Console, hub: pb_client.HubInfo) -> None:
        """Prepare a status line for one hub's session.

        Args:
            console: The terminal, already in raw mode.
            hub: The hub being talked to, for the part of the line that never
                changes.
        """
        self._console = console
        self._hub = hub
        self._status: pb_client.Status | None = None
        self._notice: str | None = None
        self._notice_until = 0.0
        self._size = (0, 0)
        self._drawn: str | None = None
        self._active = False

    # ------------------------------------------------------------- lifecycle

    def __enter__(self) -> Banner:
        """Reserve the bottom row and start drawing there.

        Returns:
            This banner.
        """
        columns, rows = self._console.size()
        self._size = (columns, rows)
        # A fresh line first, so the row about to be reserved is not the one
        # the shell's prompt is sitting on.
        self._console.write(b"\r\n")
        if rows >= 2:
            self._write(_SET_MARGIN.format(bottom=rows - 1))
            self._write(_GOTO.format(row=rows - 1))
        self._active = True
        self.draw()
        return self

    def __exit__(self, *exc: object) -> bool:
        """Give the bottom row back and leave the cursor on it.

        Synchronous, so no cancellation can skip it: a terminal left with a
        scrolling margin set is as broken as one left in raw mode, and in the
        same invisible way.

        Args:
            *exc: The exception being propagated, if any.

        Returns:
            False, so nothing is swallowed.
        """
        self._active = False
        _columns, rows = self._size
        self._write(_RESET_MARGIN)
        if rows >= 1:
            self._write(_GOTO.format(row=rows) + _CLEAR_LINE)
        return False

    # ---------------------------------------------------------------- output

    def write(self, data: bytes) -> None:
        """Put the hub's output on the screen, above the status line.

        Args:
            data: The bytes from the hub, exactly as they arrived.
        """
        self._console.write(data)

    def line(self, text: str) -> None:
        """Print one of our own lines into the session.

        Args:
            text: What to say. A carriage return is added as well as the
                newline, since output processing is off in raw mode.
        """
        self._console.write(text.encode("utf-8", "replace") + b"\r\n")

    # ---------------------------------------------------------------- status

    def update(self, status: pb_client.Status) -> None:
        """Take a new status report.

        Args:
            status: What the hub now says about itself.
        """
        self._status = status
        self.draw()

    def notice(self, text: str) -> None:
        """Say something in the status line for a few seconds.

        For the things that are worth mentioning but not worth interrupting
        the session's text for: an input the hub would not take, an unknown
        escape command.

        Args:
            text: The message.
        """
        self._notice = text
        self._notice_until = time.monotonic() + NOTICE_SECONDS
        self.draw()

    def tick(self) -> None:
        """Redraw if anything that is not an event has changed.

        Covers the two things nothing tells us about: the window being
        resized, and a passing message reaching its end. Called on a timer, so
        it must be cheap and must not draw when nothing has changed.
        """
        if self._console.size() != self._size:
            self._resize()
        elif self._notice is not None and time.monotonic() >= self._notice_until:
            self._notice = None
            self.draw()

    def _resize(self) -> None:
        """Move the status line to the new bottom row."""
        columns, rows = self._console.size()
        self._size = (columns, rows)
        self._drawn = None
        if rows >= 2:
            self._write(_SET_MARGIN.format(bottom=rows - 1))
        self.draw()

    # --------------------------------------------------------------- drawing

    def compose(self) -> str:
        """Build the status line's text.

        Returns:
            The line, before it is fitted to the terminal's width.
        """
        parts = [self._hub.name]
        if self._hub.hub_type:
            parts.append(self._hub.hub_type)
        parts.append(describe_running(self._status))
        warning = worst_warning(self._status)
        if warning:
            parts.append(warning)
        if self._notice:
            parts.append(self._notice)
        left = _SEPARATOR.join(parts)
        right = f"{escape.describe()} h for help"
        columns, _rows = self._size
        return _Layout(left, right).fit(columns)

    def draw(self) -> None:
        """Put the status line on the screen, if it has changed."""
        if not self._active:
            return
        _columns, rows = self._size
        if rows < 2:
            # No room for a line of our own. The session still works.
            return
        text = self.compose()
        if text == self._drawn:
            return
        self._drawn = text
        self._write(
            _SAVE_CURSOR
            + _GOTO.format(row=rows)
            + _CLEAR_LINE
            + _REVERSE
            + text
            + _NORMAL
            + _RESTORE_CURSOR
        )

    def _write(self, text: str) -> None:
        """Write one of our own control sequences."""
        self._console.write(text.encode("ascii", "replace"))
