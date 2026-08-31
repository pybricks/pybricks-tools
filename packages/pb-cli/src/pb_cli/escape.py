# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Telling keys meant for the hub from keys meant for ``pb``.

In a session with a hub every keystroke belongs to the hub, Ctrl-C included --
that is the point, since Ctrl-C is how a running program is interrupted. So
there has to be one key that does not, and after it a letter saying what to do.
This is the arrangement ``telnet``, ``screen``, ``picocom`` and ``microcom``
all use, and users of those tools already know the shape of it.

**Which key it is matters more here than in those tools**, because the thing on
the other end is MicroPython, and MicroPython's REPL claims an unusual number
of control characters. Its line editor uses Ctrl-A, B, D, E, F, K, N, P, U and
W, and the REPL itself gives Ctrl-A, B, C, D and E meanings of their own: raw
mode, friendly mode, interrupt, soft reset and paste mode. That rules out the
usual choices immediately -- ``screen`` and ``picocom`` escape with Ctrl-A,
which is raw REPL, and ``tmux`` with Ctrl-B, which is friendly REPL.

``Ctrl-]`` is what is left, and it is the one with the best claim anyway: it is
telnet's, so it is the most widely recognised of them, and MicroPython does not
use it for anything. Checked rather than assumed -- neither ``readline.c`` nor
``pyexec.c`` mentions ``0x1d`` at all. Sending the escape key to the hub is
still possible, by pressing it twice, which is also telnet's convention.

The matching is a byte at a time and carries its state in the value it hands
back, because the escape byte and the command byte can easily arrive in
separate reads: on
Windows the console hands over one key at a time, so a user typing at any
ordinary speed splits the pair almost always. Assuming a whole sequence arrives
in one read is the single most common bug in code like this, and a state
machine of three states is both the fix and something that can be tested
exhaustively without a terminal anywhere near it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Final

__all__ = ["ESCAPE_KEY", "Action", "Filter", "describe", "help_lines"]

ESCAPE_KEY: Final = 0x1D
"""Ctrl-], the byte that means the next key is for ``pb``.

Telnet's, and free: MicroPython gives it no meaning, so nothing is taken away
from the hub by reserving it.
"""


class Action(Enum):
    """What an escape command asks for."""

    QUIT = "quit"
    STOP = "stop"
    HELP = "help"
    UNKNOWN = "unknown"
    """A key that follows the escape and means nothing. Reported rather than
    ignored, so the user is shown the help instead of wondering."""


COMMANDS: Final[Mapping[int, Action]] = {
    ord("q"): Action.QUIT,
    ord("Q"): Action.QUIT,
    0x04: Action.QUIT,
    ord("s"): Action.STOP,
    ord("S"): Action.STOP,
    ord("h"): Action.HELP,
    ord("H"): Action.HELP,
    ord("?"): Action.HELP,
}
"""What each key after the escape does.

Ctrl-D is here as a second way to quit because it is what fingers do at a
prompt when they mean "I am finished", and on its own it belongs to the hub
where it is a soft reset.

Help is advertised as ``h`` and answers to ``?`` as well. Both are worth
having: ``?`` is what the other escape-key tools use, and ``h`` is the one a
hand can reach without a shift key.

There is deliberately no "restart the interpreter". A session that finds a
program running starts the interpreter by itself as soon as that program ends,
and a session whose interpreter stops is over, so nothing is left for a restart
command to do that the session does not already do on its own.
"""


def help_lines(key: int = ESCAPE_KEY) -> list[tuple[str, str]]:
    """List the escape commands, as a user would have to type them.

    Args:
        key: The escape byte in use.

    Returns:
        Each command's keystrokes and what it does, in the order to show them.

    """
    name = describe(key)
    return [
        (f"{name} q", "leave the session"),
        (f"{name} s", "stop what is running on the hub"),
        (f"{name} h", "show this help"),
        (f"{name} {name}", f"send {name} to the hub"),
    ]


def describe(key: int = ESCAPE_KEY) -> str:
    """Name a control byte the way a person would say it.

    Args:
        key: The byte.

    Returns:
        Something like ``Ctrl-]``.
    """
    if 0 < key < 0x20:
        return f"Ctrl-{chr(key + 0x40)}"
    return repr(chr(key))


@dataclass(frozen=True, slots=True)
class Filter:
    """Splits what was typed into bytes for the hub and commands for ``pb``.

    A value rather than something a session edits: :meth:`feed` returns the
    filter to read the *next* keys with, so what a half-finished escape needs
    is carried in the value rather than accumulated in an object. That is worth
    the extra name at the call site, because a read ending between the escape
    key and its command is the ordinary case and not an edge one -- a console
    hands over one key at a time, so the two halves nearly always arrive in
    separate reads -- and a value makes each step of that a thing a test can
    state on one line.
    """

    key: int = ESCAPE_KEY
    """The byte that starts a command."""

    armed: bool = False
    """Whether the escape key has been seen and its command is awaited."""

    def feed(self, data: bytes) -> tuple[Filter, bytes, tuple[Action, ...]]:
        """Sort one read from the terminal.

        Args:
            data: The bytes just typed.

        Returns:
            The filter to read the next keys with, what to send to the hub,
            and the commands that were asked for, in the order they were
            typed.
        """
        armed = self.armed
        for_hub = bytearray()
        actions: list[Action] = []

        for byte in data:
            if armed:
                armed = False
                if byte == self.key:
                    # Pressed twice, so it was meant for the hub after all.
                    for_hub.append(byte)
                else:
                    actions.append(COMMANDS.get(byte, Action.UNKNOWN))
            elif byte == self.key:
                armed = True
            else:
                for_hub.append(byte)

        # Unchanged is the common case, and a value that did not change has no
        # reason to be a different object.
        rest = self if armed == self.armed else replace(self, armed=armed)
        return rest, bytes(for_hub), tuple(actions)
