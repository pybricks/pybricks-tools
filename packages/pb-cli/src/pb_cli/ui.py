# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What the commands put on screen.

Everything a command draws goes through here, for two reasons. Commands stay
thin, which is what keeps them readable as "ask the service, then say what
happened"; and the whole of the tool's appearance can be redirected at once,
which is how :mod:`tools.screenshot` records a real session for the README.

One rule governs all of it. **Data goes to stdout and everything else goes to
stderr** -- progress, spinners, tables meant to be looked at rather than
parsed, prompts, errors. That single split is what keeps ``pb list | jq`` and
``pb scan > hubs.txt`` working, and it is why :func:`console` takes ``err``
rather than being one console everything shares.

The second rule is that anything drawn with colour must also read without it.
A terminal that strips styling, a log file, a reader who cannot tell green from
red: none of them should lose information. So the signal meter varies its
characters and not only its colour, and the chooser marks its selected row with
an arrow as well as a style.
"""

from __future__ import annotations

import contextlib
from collections.abc import Collection, Generator, Iterable, Mapping
from contextlib import AbstractContextManager
from typing import Final

from rich.console import Console, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

import pb_client
from pb_cli import errors

__all__ = [
    "HUB_WARNINGS",
    "SELECTED_MARK",
    "SHOW_HUB_IDS",
    "SIGNAL_BLOCKS",
    "SIGNAL_EMPTY",
    "chooser_table",
    "connected_table",
    "console",
    "hub_warnings",
    "live",
    "plain",
    "rows",
    "scan_table",
    "signal_bars",
    "spinner",
    "usage",
]

SHOW_HUB_IDS: bool = False
"""Whether to print hub ids.

Off for now. A hub id is built from its Bluetooth address, which is not stable
-- and on macOS is a per-machine UUID standing in for one -- so printing it
invites users to write down something that will stop working. Ids remain in the
wire protocol and are still accepted as arguments; only the display is off.

Turn this on once a hub has a durable identifier worth showing -- see "Wanted
from the firmware" in the design notes, since the hub does not have one yet.
Annotated as ``bool`` rather than left to inference so that the code behind it
keeps being type-checked while it is switched off.
"""

SIGNAL_BLOCKS = "▁▃▅▇"
"""The signal meter, one character per level, rising left to right."""

SIGNAL_EMPTY = "·"
"""What stands in for a bar that is not lit.

A dim copy of the block would say the same thing in colour alone, which is no
use to a colour-blind reader or to anything that strips styling. A dot is
plainly not a bar.
"""

SELECTED_MARK = "▸"
"""Marks the row a chooser is on, so the selection reads without colour."""

_SIGNAL_FLOORS = (-90, -80, -67, -55)
"""How many dBm each level needs, weakest first.

Bluetooth reports signal strength as a negative number of dBm, roughly -30 for
a hub on the desk down to about -100 for one that can barely be heard. The
boundaries are the ones Wi-Fi tools have long used, which put a hub in the same
room at four bars and one through a wall at two. Anything at or below the first
floor still gets one bar rather than none: it was heard, so it is there.
"""

HUB_WARNINGS: Final[Mapping[str, str]] = {
    "BATTERY_LOW_VOLTAGE_WARNING": "battery low",
    "BATTERY_LOW_VOLTAGE_SHUTDOWN": "battery empty, the hub is switching off",
    "BATTERY_HIGH_CURRENT": "drawing too much current",
    "BATTERY_HIGH_TEMP_WARNING": "battery hot",
    "BATTERY_HIGH_TEMP_SHUTDOWN": "battery too hot, the hub is switching off",
    "BLE_LOW_SIGNAL": "weak Bluetooth signal",
    "SHUTDOWN_REQUEST": "switching off",
    "SHUTDOWN": "switching off",
}
"""Hub status flags that are worth saying out loud, in the user's words.

Keyed on the wire name of the flag rather than on the bit, because the CLI
deliberately knows nothing about the hub protocol. Flags that describe a normal
state -- advertising, connected, a program running -- are missing on purpose:
they are either uninteresting or already a line of their own.
"""


def console(*, err: bool = False) -> Console:
    """Build a console for data (stdout) or for everything else (stderr).

    Args:
        err: True for the console that progress, prompts and messages go to.

    Returns:
        A fresh console. Fresh rather than shared so that a caller can be
        given a different one -- which is how the session in the README is
        recorded.
    """
    # Highlighting off. Rich's default highlighter reads a string as a Python
    # repr and colours what it finds there, which is right for a value dumped
    # to a terminal and wrong for a sentence: "firmware 4.1.0b3-134-gb4bd99edd"
    # came out with "4.1" and "134" in cyan and everything between them plain,
    # as though the version were two numbers with debris around them, and every
    # bracketed aside was drawn in bold. Nothing printed here is a repr -- data
    # goes to stdout through `plain` and `rows`, which do not use print at all
    # -- so any emphasis in this output should be markup we chose.
    return Console(stderr=err, highlight=False)


@contextlib.contextmanager
def _null() -> Generator[None]:
    """Stand in for a spinner when output is not a terminal."""
    yield


def spinner(err: Console, message: str) -> AbstractContextManager[object]:
    """Show progress while a step runs.

    Args:
        err: The stderr console. Progress is never data.
        message: What is happening, in the present tense.

    Returns:
        A context manager, which does nothing at all when stderr is not a
        terminal -- a spinner in a log file is just escape codes.
    """
    return err.status(message) if err.is_terminal else _null()


def live(renderable: RenderableType, target: Console) -> Live:
    """Open a display that is redrawn in place.

    Args:
        renderable: The first frame.
        target: Where to draw.

    Returns:
        The display. Manual refresh, because every caller here redraws in
        response to something arriving rather than on a clock.
    """
    return Live(renderable, console=target, auto_refresh=False)


def usage(message: str, hint: str | None = None) -> int:
    """Report a problem with the command line itself.

    Presented like any other expected failure -- one sentence and a suggestion
    -- rather than through a cyclopts group validator, whose wording ("Invalid
    values for group Program...") is written for whoever wrote the CLI rather
    than for whoever is using it.

    Args:
        message: What is wrong with the arguments.
        hint: What to type instead.

    Returns:
        The process exit code.
    """
    return errors.render(console(err=True), [errors.Problem(message, hint)])


_TRANSPORT_NAMES: Final[Mapping[str, str]] = {
    "ble": "Bluetooth",
    "usb": "USB",
    "fake": "simulated",
}
"""What to call each way of reaching a hub.

The service's words are ``ble`` and ``usb``, which are the right names on the
wire and the wrong ones on a screen: nobody calls it BLE except us.
"""


def transport_name(transport: str) -> str | None:
    """Say how a hub is reached, in a user's words.

    Args:
        transport: What the service reported.

    Returns:
        The wording, or ``None`` if the service said nothing -- which is what an
        older service looks like, and is a reason to leave the line out rather
        than to show an empty one. An unrecognised transport is passed through
        as it came, since a name we do not have is still better than no answer.
    """
    if not transport:
        return None

    return _TRANSPORT_NAMES.get(transport, transport)


def hub_warnings(status_names: Iterable[str]) -> list[str]:
    """Translate the hub's status flags into things worth telling the user.

    Args:
        status_names: Flag names as the hub reported them.

    Returns:
        The wording for each flag that says something actionable, in the order
        given and without repeats -- two flags can mean the same thing to a
        user, and saying it twice would look like a fault in the tool.
    """
    said: list[str] = []

    for name in status_names:
        warning = HUB_WARNINGS.get(name)
        if warning is not None and warning not in said:
            said.append(warning)

    return said


def signal_bars(rssi: int | None) -> str:
    """Draw a signal strength as a bar chart, in Rich markup.

    Args:
        rssi: The received signal strength in dBm, as Bluetooth reports it, or
            ``None`` when the transport does not measure one.

    Returns:
        Four characters wide however strong the signal, so that rows stay
        aligned. A number in dBm means nothing to most people, but "how many
        bars" is something everyone already reads off a phone.
    """

    if rssi is None:
        return f"[dim]{SIGNAL_EMPTY * len(SIGNAL_BLOCKS)}[/]"

    # At least one bar: the hub was heard, so it is out there somewhere.
    level = max(1, sum(1 for floor in _SIGNAL_FLOORS if rssi >= floor))
    style = "green" if level >= 3 else "yellow" if level == 2 else "red"
    filled = SIGNAL_BLOCKS[:level]
    empty = SIGNAL_EMPTY * (len(SIGNAL_BLOCKS) - level)
    return f"[{style}]{filled}[/][dim]{empty}[/]"


def plain(out: Console, line: str) -> None:
    """Write one line to be read by another program, exactly as given.

    Not through :meth:`rich.console.Console.print`, which is for text meant for
    a person. Rich expands tabs as it renders and wraps at the console width --
    80 columns when it cannot see a terminal -- so a tab-separated line printed
    through it arrives with its tabs turned into spaces, and a long one arrives
    folded in half. Both quietly break ``cut -f2`` and everything like it, and
    neither shows up until somebody pipes the output somewhere.

    Flushed, because some of what is written this way is written while a
    command is still running -- hubs as a scan hears them, lines as the service
    logs them -- and a pipe buffers in blocks. Without this, ``pb scan |
    head -1`` waits for the scan to end rather than answering the moment there
    is an answer, and ``pb service log -f`` piped anywhere shows nothing at all
    until it is stopped. The cost is nothing at these volumes: a hub's own
    output does not come this way.

    Args:
        out: The stdout console. Only data is written this way.
        line: Exactly what to write, without its newline.
    """
    out.file.write(line + "\n")
    out.file.flush()


def rows(out: Console, pairs: list[tuple[str, str]]) -> None:
    """Render key/value rows.

    Args:
        out: Where to write. This is data, so it is stdout.
        pairs: Name and value for each row, in the order to show them.
    """

    if out.is_terminal:
        table = Table(show_header=False, box=None)

        for key, value in pairs:
            table.add_row(key, value)

        out.print(table)
    else:
        # Tab-separated when piped, so cut and awk can read it.
        for key, value in pairs:
            plain(out, f"{key}\t{value}")


def scan_table(
    devices: Iterable[pb_client.DeviceInfo],
    caption: str = "",
    selected: int | None = None,
    connected: Collection[str] = (),
) -> Table:
    """Build the table of hubs seen so far.

    Args:
        devices: What has been seen, in the order it was first seen. Not sorted
            by signal, tempting as that is: rows that swap places under the
            reader's eyes while they are trying to read one are worse than a
            list that is not in the ideal order.
        caption: A line under the table saying what is happening.
        selected: Which row the user is on, when this is a chooser rather than
            a report. Marked with an arrow as well as with a style, so it is
            still visible where colour is not.
        connected: The ids of hubs that are connected. A connected hub does
            not advertise, so there is no signal to report for one and the
            column says so instead of drawing an empty meter.

    Returns:
        The table, ready to print or hand to a live display.
    """
    table = Table(caption=caption or None, caption_justify="left")

    if selected is not None:
        table.add_column("")

    table.add_column("Name")
    # Known before connecting, because a Pybricks hub puts its Plug and Play ID
    # in the scan response. Two hubs called "Pybricks Hub" are told apart by
    # this and by their signal, which is the whole reason to show either.
    table.add_column("Type")

    if SHOW_HUB_IDS:
        table.add_column("Address", style="dim")

    table.add_column("Signal", justify="right")

    for index, device in enumerate(devices):
        strength = (
            "[green]connected[/]"
            if device.id in connected
            else signal_bars(device.rssi)
        )
        row = [device.name, device.hub_type or "", device.id, strength]

        if not SHOW_HUB_IDS:
            del row[2]

        if selected is not None:
            row.insert(0, SELECTED_MARK if index == selected else " ")

        table.add_row(*row, style="bold" if index == selected else None)

    return table


def connected_table(hubs: Iterable[pb_client.HubInfo], acting: str | None) -> Table:
    """Build the table of hubs the service is holding open.

    Args:
        hubs: The connected hubs.
        acting: The id of the one a command with no hub named would act
            on, marked with a star.

    Returns:
        The table.

    There is no state column. This lists what is connected, so it could only
    ever say "connected"; it comes back if the service ever tracks hubs it
    knows about but is not holding.
    """
    table = Table()
    table.add_column("")
    table.add_column("Name")
    table.add_column("Firmware")
    table.add_column("Used by", justify="right")

    for info in hubs:
        table.add_row(
            "*" if info.id == acting else "",
            info.name,
            info.firmware,
            str(info.subscribers),
        )

    return table


def chooser_table(
    devices: list[pb_client.DeviceInfo],
    selected: int,
    connected: Collection[str] = (),
) -> RenderableType:
    """Draw the chooser's list.

    Args:
        devices: The hubs heard so far.
        selected: Which one the user is on.
        connected: The ids of hubs that are connected rather than heard.

    Returns:
        What to show, which is a line of text until a hub has been heard -- an
        empty table with headings would look like a list of no hubs rather than
        like still looking for them.
    """

    if not devices:
        return Text("Looking for hubs...", style="dim")

    return scan_table(devices, selected=selected, connected=connected)
