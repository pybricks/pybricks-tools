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

**Stopping has a second way in, because it is the one done in a hurry.**
``Ctrl-] s`` is a modifier, a bracket and a remembered letter at the moment
somebody wants their robot to stop, so :data:`STOP_KEY` -- Escape, pressed
twice -- does the same thing. It is one key, it is on every keyboard, and a
hand finds it without looking.

Escape is also the first byte of every arrow key, and the hub's line editor
needs those, so the two have to be told apart. **Two Escapes in a row are
unambiguous in the byte stream**: a terminal's escape sequences are always
Escape followed by an introducer -- ``[``, ``O`` and the like -- so no sequence
can contain two Escapes running, and no timing window is needed to know which
this is. That is the whole reason it is two presses rather than one; one press
could only be told from an arrow key by how long nothing followed it, and a
window is a thing that is wrong occasionally rather than never.

The cost is that a lone Escape is held back until the next byte arrives, and
that is imperceptible: the hub gets both together, and its line editor was
waiting for the second byte anyway. What is left is Alt-Escape and the habit
of pressing Escape twice in ``vi``, neither of which means anything at a
MicroPython prompt, and both of which land on the safe side -- for a robot, an
unasked-for stop is the better way to be wrong.

The matching is a byte at a time and carries its state in the value it hands
back, because both pairs can easily arrive in separate reads: on Windows the
console hands over one key at a time, so a user typing at any ordinary speed
splits them almost always. Assuming a whole sequence arrives in one read is the
single most common bug in code like this, and a state machine of three states
-- nothing pending, a command awaited, an Escape held -- is both the fix and
something that can be tested exhaustively without a terminal anywhere near it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Final

__all__ = ["ESCAPE_KEY", "STOP_KEY", "Action", "Filter", "describe", "help_lines"]

ESCAPE_KEY: Final = 0x1D
"""Ctrl-], the byte that means the next key is for ``pb``.

Telnet's, and free: MicroPython gives it no meaning, so nothing is taken away
from the hub by reserving it.
"""

STOP_KEY: Final = 0x1B
"""Escape, which stops what is running on the hub when pressed twice.

Not taken from the hub, because one press still reaches it -- held only until
the key after it says whether it began a sequence. See the module docstring for
why two presses can be recognised with no timing at all.
"""

_NAMES: Final[Mapping[int, str]] = {STOP_KEY: "Esc"}
"""Keys with a name of their own, rather than the Ctrl-something they encode.

Escape is Ctrl-[ as far as the byte is concerned, and nobody has called it that
since terminals had a key marked Meta.
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


def help_lines(
    key: int = ESCAPE_KEY, stop_key: int = STOP_KEY
) -> list[tuple[str, str]]:
    """List the escape commands, as a user would have to type them.

    Args:
        key: The escape byte in use.
        stop_key: The byte that stops the hub when pressed twice.

    Returns:
        Each command's keystrokes and what it does, in the order to show them.
        Stopping comes first, because it is the one somebody is reading this
        list in a hurry to find.
    """
    name = describe(key)
    stop = describe(stop_key)
    return [
        (f"{stop} {stop}", "stop what is running, ending the session"),
        (f"{name} q", "leave the session"),
        (f"{name} s", f"the same as {stop} {stop}"),
        (f"{name} h", "show this help"),
        (f"{name} {name}", f"send {name} to the hub"),
    ]


def describe(key: int = ESCAPE_KEY) -> str:
    """Name a control byte the way a person would say it.

    Args:
        key: The byte.

    Returns:
        Something like ``Ctrl-]``, or the key's own name where it has one.
    """
    named = _NAMES.get(key)
    if named is not None:
        return named

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

    stop_key: int = STOP_KEY
    """The byte that stops the hub when it arrives twice running."""

    armed: bool = False
    """Whether the escape key has been seen and its command is awaited."""

    held: bool = False
    """Whether a :attr:`stop_key` is being held back to see what follows it.

    One press belongs to the hub and two are the stop, and which it was is
    known as soon as the next byte arrives -- so the first one waits rather
    than being guessed at. Nothing is lost by waiting: the hub's line editor
    reads an escape as the start of a sequence and is waiting for that same
    byte itself.
    """

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
        held = self.held
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

                continue

            if held:
                held = False

                if byte == self.stop_key:
                    actions.append(Action.STOP)

                    continue

                # Whatever this is, it is not a second press, so the one being
                # held belonged to the hub -- and this byte is then read as if
                # it had never been held at all, since it may perfectly well be
                # the escape key starting a command of its own.
                for_hub.append(self.stop_key)

            if byte == self.key:
                armed = True
            elif byte == self.stop_key:
                held = True
            else:
                for_hub.append(byte)

        # Unchanged is the common case, and a value that did not change has no
        # reason to be a different object.
        if armed == self.armed and held == self.held:
            return self, bytes(for_hub), tuple(actions)

        return replace(self, armed=armed, held=held), bytes(for_hub), tuple(actions)
