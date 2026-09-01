# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The terminal, in raw mode, as bytes in and bytes out.

This is the piece :mod:`pb_cli.keys` deliberately is not. A chooser only has to
tell up from down from Enter; a session with a hub has to deliver *every* byte
a key produces, unchanged, including the ones that would otherwise be a signal
-- Ctrl-C is a byte for the interpreter on the hub, not an interrupt for us.

Three properties are what make this tractable, and each one removes a class of
bug that hand-rolled terminal code is famous for.

**Waiting is waiting, and reading never blocks.** The two are separate calls:
:meth:`Console.wait` returns once there may be something to read, and
:meth:`Console.read` takes whatever is there without ever blocking. So a
keystroke wakes the session in microseconds rather than at the next turn of a
timer, and nothing anywhere polls the keyboard. Splitting them is what makes
that possible on both platforms at once, because the two arrive at it
differently: POSIX waits on the file descriptor itself, and Windows waits on a
console handle in a thread, since no event loop will do it. Neither ever blocks
inside the read, which is the thing that cannot be cancelled.

**Windows and POSIX produce the same bytes.** Setting
``ENABLE_VIRTUAL_TERMINAL_INPUT`` makes the console encode special keys as the
escape sequences a terminal would send, so an arrow key really does arrive as
``ESC [ A`` on both. Measured, not assumed. Everything above this package is
therefore shared, and none of the vkey-to-sequence translation tables that
other tools carry is needed.

**Leaving raw mode is a synchronous ``__exit__``.** A sync exit cannot be
cancelled at all, which is stronger than shielding, so the terminal is restored
on every ordinary path -- return, exception, cancellation -- by the language
itself rather than by anything this module remembers to do.
"""

from __future__ import annotations

import sys
from typing import Protocol, runtime_checkable

if sys.platform == "win32":
    from pb_cli.console.windows import platform_problem, raw_console

else:
    from pb_cli.console.posix import platform_problem, raw_console

__all__ = ["Console", "available", "raw_console", "why_unavailable"]


@runtime_checkable
class Console(Protocol):
    """A terminal that has been put into raw mode."""

    async def wait(self) -> None:
        """Wait until there may be something to read.

        Returns when the terminal has input, which is not a promise that
        :meth:`read` will yield any bytes: a console reports more than typing,
        and the window merely gaining focus counts as input here. The caller
        reads, gets nothing, and waits again.
        """
        ...

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

    return platform_problem()


def available() -> bool:
    """Whether an interactive session is possible here.

    Returns:
        True if this process has a terminal it can put into raw mode.
    """
    return why_unavailable() is None
