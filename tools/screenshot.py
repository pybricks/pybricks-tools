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
import time
from collections.abc import Awaitable, Callable, Generator
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
from rich.console import Console, RenderableType

import pb_client
from pb import commands_hub, commands_program, keys, ui
from pb.keys import Key

WIDTH = 74
"""Columns to render, chosen to fit a README on GitHub without scrolling."""

OUT = Path("docs/assets/pb.svg")

TARGET = "City hub"
"""The kind of hub the picture connects to.

Named rather than left to whichever hub answered first, so that the program in
the next line can name its hub class. ``ThisHub`` would run anywhere and would
save this trouble, but it is not what anybody writes: a reader who has just
watched a City hub being chosen should see ``CityHub`` in the program that
follows.
"""

RAINBOW = """from pybricks.hubs import CityHub
from pybricks.parameters import Color
from pybricks.tools import wait

hub = CityHub()
for hue in range(0, 360, 2):
    hub.light.on(Color(hue))
    wait(20)
"""
"""The program the picture runs, on the hub the picture connects to.

Every hub has a colour light and nothing else is plugged in, so the only thing
tying this to one kind of hub is the class name -- which is the point: it has
to match what was chosen a moment earlier.

Kept in step with the README by hand, which is safe enough because it is what
this script actually sends to the hub -- if it stopped working, the picture
would show it failing.
"""

SESSION: list[tuple[str, Callable[[], Awaitable[int]]]] = [
    ("pb connect --scan", lambda: commands_hub.connect_cmd(scan=True)),
    (f'pb run -c "\n{RAINBOW}"', lambda: commands_program.run_cmd(command=RAINBOW)),
]
"""What to show, in the order a new user would type it.

Two commands, because that is genuinely all it takes. `pb connect --scan` is
the first thing most people will type, and it is also the best thing to look
at: the list it shows names each hub, says what kind it is, and meters its
signal.
"""

NICKNAMES = {"prime_hub": "Bumblebee", "Pybricks Hub": "Rover"}
"""What to call the hubs in the picture, instead of what they are really called.

The hubs to hand are named after their firmware, which is what a hub is called
before anyone has given it a name and is a dull thing to put in a picture. What
someone reading the README should see is what their own hubs will look like
once they have named them after whatever they built.

Substituted where a hub's details are decoded, so it applies everywhere a name
is printed and nothing has to know about it. Display only: nothing in this
session looks a hub up by name, so no lookup is being lied to.
"""

TYPING_DELAY = 3.0
"""How long to leave the chooser alone before pressing anything.

Long enough for every hub in the room to have advertised at least once, so the
picture shows a full list rather than whichever hub happened to be quickest.
"""


class _StillLive:
    """Stand in for a live display, keeping only its last frame.

    A recording console keeps every frame a :class:`rich.live.Live` draws, so
    the picture would show the scan's table half a dozen times over. A still
    picture of a live display is its final frame, which is what this keeps.
    """

    def __init__(self, renderable: object, target: Console) -> None:
        """Start with the display's first frame.

        Takes the same arguments as :func:`pb.ui.live`, which it stands in for.

        Args:
            renderable: What the display starts out showing.
            target: Where the last frame is written.
        """
        self._console = target
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


class _Keystrokes:
    """Work the chooser: wait, walk the selection onto a hub, then take it.

    Driven by what is actually on screen rather than by a fixed number of
    presses, because which hub is heard first varies from run to run and the
    picture has to end up on the same one every time -- the program it runs
    next names that hub's class.

    Watches the list by standing in front of the function that draws it, which
    is the only place the selection is visible from outside.
    """

    def __init__(self, target: str = TARGET, after: float = TYPING_DELAY) -> None:
        """Set up the typing.

        Args:
            target: The kind of hub to end up on, as the Type column spells it.
            after: Seconds to leave the chooser alone first, so that every hub
                in the room has had a chance to advertise.
        """
        self._target = target
        self._after = after
        self._start: float | None = None
        self._listed: list[pb_client.DeviceInfo] = []
        self._selected = 0
        self._taken = False

    def watching(
        self, draw: Callable[[list[pb_client.DeviceInfo], int], RenderableType]
    ) -> Callable[[list[pb_client.DeviceInfo], int], RenderableType]:
        """Wrap the chooser's drawing function so that this can see the list.

        Args:
            draw: What actually draws the list.

        Returns:
            The same function, noting what it was asked to draw on the way.
        """

        def drawing(
            devices: list[pb_client.DeviceInfo], selected: int
        ) -> RenderableType:
            self._listed, self._selected = devices, selected
            return draw(devices, selected)

        return drawing

    def __call__(self) -> Key | None:
        """Return the next key, or nothing if there is nothing to press yet.

        Returns:
            The key to deliver to the chooser this poll.
        """
        now = time.monotonic()
        if self._start is None:
            self._start = now
        if now - self._start < self._after or self._taken:
            return None
        wanted = next(
            (
                index
                for index, device in enumerate(self._listed)
                if device.hub_type == self._target
            ),
            None,
        )
        if wanted is None:
            # Not heard yet. Keep waiting rather than settling for another.
            return None
        if wanted != self._selected:
            return Key.DOWN
        self._taken = True
        return Key.ENTER


_REAL_DEVICE = pb_client.DeviceInfo.from_wire
_REAL_HUB = pb_client.HubInfo.from_wire


def _nickname(name: str) -> str:
    """Return what to call a hub in the picture."""
    return NICKNAMES.get(name, name)


def _named_device(payload: dict[str, Any]) -> pb_client.DeviceInfo:
    """Decode a scanned hub, under its nickname."""
    return replace(_REAL_DEVICE(payload), name=_nickname(str(payload.get("name", ""))))


def _named_hub(payload: dict[str, Any]) -> pb_client.HubInfo:
    """Decode a connected hub, under its nickname."""
    return replace(_REAL_HUB(payload), name=_nickname(str(payload.get("name", ""))))


@contextlib.contextmanager
def _capture(console: Console) -> Generator[None]:
    """Redirect everything the picture depends on, for the duration of a session.

    Three separate things, all restored on the way out: where the commands
    draw, how their animations behave in a still picture, and -- for the
    chooser -- the keystrokes and the hub names.
    """
    original = (
        ui.console,
        ui.spinner,
        ui.live,
        ui.chooser_table,
        keys.poll_key,
        keys.reading_keys,
        keys.keys_available,
    )
    ui.console = lambda *, err=False: console
    ui.spinner = lambda err, message: contextlib.nullcontext()
    ui.live = _StillLive
    typing = _Keystrokes()
    keys.poll_key = typing
    ui.chooser_table = typing.watching(ui.chooser_table)
    # No terminal to put into cbreak mode, and no terminal to check for.
    keys.reading_keys = contextlib.nullcontext
    keys.keys_available = lambda: True
    pb_client.DeviceInfo.from_wire = _named_device
    pb_client.HubInfo.from_wire = _named_hub
    try:
        yield
    finally:
        (
            ui.console,
            ui.spinner,
            ui.live,
            ui.chooser_table,
            keys.poll_key,
            keys.reading_keys,
            keys.keys_available,
        ) = original
        pb_client.DeviceInfo.from_wire = _REAL_DEVICE
        pb_client.HubInfo.from_wire = _REAL_HUB


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
