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

It reaches into two private names in :mod:`pb.cli`, which nothing else does and
nothing else should. ``_console`` is the seam every command gets its output
through, so replacing it is what captures the output at all; and ``_spinner``
has to go, because a still picture of a spinner is a stray line of dots.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator
from pathlib import Path

from rich.console import Console

from pb import cli

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

SESSION: list[tuple[str, Callable[[], object]]] = [
    ("pb connect", cli.connect_cmd),
    (f'pb run -c "\n{RAINBOW}"', lambda: cli.run_cmd(command=RAINBOW)),
    ("pb status", cli.status_cmd),
]
"""What to show, in the order a new user would type it."""


@contextlib.contextmanager
def _capture(console: Console) -> Generator[None]:
    """Point every command's output at one console, and stop the spinners."""
    original_console, original_spinner = cli._console, cli._spinner
    cli._console = lambda *, err=False: console
    cli._spinner = lambda err, message: contextlib.nullcontext()
    try:
        yield
    finally:
        cli._console, cli._spinner = original_console, original_spinner


def _prompt(console: Console, typed: str) -> None:
    """Print a command the way a shell shows it, continuation lines and all."""
    first, *rest = typed.splitlines()
    console.print(f"[bold green]$[/] [bold]{first}[/]", highlight=False)
    for line in rest:
        console.print(f"[dim]>[/] [bold]{line}[/]", highlight=False)


def main() -> int:
    """Record a session and write the SVG.

    Returns:
        The process exit code.
    """
    console = Console(record=True, force_terminal=True, width=WIDTH)
    with _capture(console):
        for typed, command in SESSION:
            _prompt(console, typed)
            command()
            console.print()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(console.export_svg(title="pb"), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
