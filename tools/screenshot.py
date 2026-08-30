# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Render the terminal picture at the top of the README.

This runs the real commands against a real hub and records what they print, so
the picture cannot drift from what the tool actually does. Rich renders the
recording as an SVG, which GitHub displays inline and which stays crisp at any
size -- a PNG screenshot would be a picture of one particular terminal's fonts
and theme.

Everything shown works with nothing but the installed command and a hub: no
clone, no example file, no motors or sensors plugged in. That is the same bar
the README itself has to meet.

Connect a hub first, then::

    uv run python tools/screenshot.py

It works by redirecting :mod:`pb.ui`, which exists to be the one place every
command draws through. ``console`` is what captures the output at all;
``spinner`` and ``live`` are replaced because a still picture of an animation
is either a stray line of dots or the same table half a dozen times over.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Generator
from pathlib import Path

import anyio
from rich.console import Console

from pb import commands_hub, commands_program, ui

WIDTH = 74
"""Columns to render, chosen to fit a README on GitHub without scrolling."""

OUT = Path("docs/assets/pb.svg")

RAINBOW = """from pybricks.hubs import ThisHub
from pybricks.parameters import Color
from pybricks.tools import wait

hub = ThisHub()
for hue in range(0, 360, 2):
    hub.light.on(Color(hue))
    wait(20)
"""
"""A program every hub can run: they all have a colour light and nothing else.

Kept in step with the README by hand, which is safe enough because it is what
this script actually sends to the hub -- if it stopped working, the picture
would show it failing.
"""

SESSION: list[tuple[str, Callable[[], Awaitable[int]]]] = [
    ("pb scan --timeout 5", lambda: commands_hub.scan_cmd(timeout=5)),
    ("pb connect", commands_hub.connect_cmd),
    (f'pb run -c "\n{RAINBOW}"', lambda: commands_program.run_cmd(command=RAINBOW)),
]
"""What to show, in the order a new user would type it."""


class _StillLive:
    """Stand in for a live display, keeping only its last frame.

    A recording console keeps every frame a :class:`rich.live.Live` draws, so
    the picture would show the scan's table half a dozen times over. A still
    picture of a live display is its final frame, which is what this keeps.
    """

    def __init__(self, renderable: object, *, console: Console, **_: object) -> None:
        """Start with the display's first frame.

        Args:
            renderable: What the display starts out showing.
            console: Where the last frame is written.
            **_: Whatever else rich's own Live is given, which does not matter
                when nothing is being animated.
        """
        self._console = console
        self._frame = renderable

    def __enter__(self) -> _StillLive:
        """Return self, as rich's Live does.

        Returns:
            This display.
        """
        return self

    def __exit__(self, *exc: object) -> bool:
        """Print the frame the display ended on.

        Args:
            *exc: The exception being propagated, if any.

        Returns:
            False, so nothing is swallowed.
        """
        self._console.print(self._frame)
        return False

    def update(self, renderable: object, **_: object) -> None:
        """Replace the frame.

        Args:
            renderable: The new frame.
            **_: Rich's refresh flag, which means nothing here.
        """
        self._frame = renderable


@contextlib.contextmanager
def _capture(console: Console) -> Generator[None]:
    """Point every command's output at one console, and still its animations."""
    original = ui.console, ui.spinner, ui.live
    ui.console = lambda *, err=False: console
    ui.spinner = lambda err, message: contextlib.nullcontext()
    ui.live = _StillLive
    try:
        yield
    finally:
        ui.console, ui.spinner, ui.live = original


def _prompt(console: Console, typed: str) -> None:
    """Print a command the way a shell shows it, continuation lines and all."""
    first, *rest = typed.splitlines()
    console.print(f"[bold green]$[/] [bold]{first}[/]", highlight=False)
    for line in rest:
        console.print(f"[dim]>[/] [bold]{line}[/]", highlight=False)


async def main() -> int:
    """Record a session and write the SVG.

    Returns:
        The process exit code.
    """
    console = Console(record=True, force_terminal=True, width=WIDTH)
    with _capture(console):
        for typed, command in SESSION:
            _prompt(console, typed)
            await command()
            console.print()

    await anyio.Path(OUT.parent).mkdir(parents=True, exist_ok=True)
    await anyio.Path(OUT).write_text(console.export_svg(title="pb"), encoding="utf-8")
    return 0


if __name__ == "__main__":
    # The one place this program enters an event loop, as cyclopts is for
    # the command line proper. The commands themselves start none.
    raise SystemExit(anyio.run(main))
