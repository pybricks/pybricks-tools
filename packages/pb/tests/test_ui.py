# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What the commands draw, on a terminal and down a pipe.

Both are tested, because they are different code and only one of them is ever
looked at while developing. The piped form is the one with a contract: another
program is reading it.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

import pb_client
from pb import ui

WIDE = 200
"""Wider than any row here, so nothing wraps for reasons of its own."""


def _terminal() -> Console:
    """A console that behaves as if it were a terminal, and records what it drew."""
    return Console(record=True, force_terminal=True, width=WIDE, file=io.StringIO())


def _piped() -> tuple[Console, io.StringIO]:
    """A console writing somewhere that is not a terminal, and its buffer."""
    buffer = io.StringIO()
    return Console(file=buffer, width=WIDE), buffer


def _device(
    name: str = "Pybricks Hub",
    rssi: int | None = -50,
    hub_type: str | None = "City hub",
) -> pb_client.DeviceInfo:
    return pb_client.DeviceInfo(
        id=f"fake:{name}",
        name=name,
        address="00:11:22:33:44:55",
        rssi=rssi,
        transport="fake",
        hub_type=hub_type,
    )


def _hub(name: str = "Pybricks Hub", subscribers: int = 1) -> pb_client.HubInfo:
    return pb_client.HubInfo(
        id=f"fake:{name}",
        name=name,
        state="connected",
        hub_type="City hub",
        firmware="4.0.1",
        protocol="1.5.0",
        address="00:11:22:33:44:55",
        num_slots=20,
        max_program_size=10000,
        subscribers=subscribers,
        program_running=False,
        status_names=(),
    )


# ------------------------------------------------------------------- piped


def test_piped_rows_are_tab_separated() -> None:
    # The contract for anything reading this with cut or awk. Rich expands
    # tabs when it renders, so this only holds because the piped path does not
    # go through it.
    out, buffer = _piped()
    ui.rows(out, [("name", "Pybricks Hub"), ("state", "connected")])
    assert buffer.getvalue() == "name\tPybricks Hub\nstate\tconnected\n"


def test_a_long_piped_line_is_not_folded() -> None:
    # Rich wraps at the console width, which is 80 when it cannot see a
    # terminal. A wrapped path or hub name would be silently corrupt.
    out, buffer = _piped()
    long_value = "x" * 500
    ui.plain(out, f"log\t{long_value}")
    assert buffer.getvalue() == f"log\t{long_value}\n"


def test_piped_output_has_no_escape_codes() -> None:
    # Colour in a file is noise that every reader then has to strip.
    out, buffer = _piped()
    ui.rows(out, [("signal", ui.signal_bars(-50))])
    assert "\x1b" not in buffer.getvalue()


# ---------------------------------------------------------------- terminal


def test_a_scan_table_names_the_hub_its_type_and_its_signal() -> None:
    console = _terminal()
    console.print(ui.scan_table([_device()], "Found 1."))
    drawn = console.export_text()
    assert "Pybricks Hub" in drawn
    assert "City hub" in drawn
    assert ui.SIGNAL_BLOCKS[0] in drawn
    assert "Found 1." in drawn


def test_a_hub_that_does_not_say_what_it_is_still_gets_a_row() -> None:
    # Not knowing the kind is no reason to hide a hub that is plainly there.
    console = _terminal()
    console.print(ui.scan_table([_device(hub_type=None)]))
    assert "Pybricks Hub" in console.export_text()


def test_the_chooser_marks_the_row_it_is_on() -> None:
    console = _terminal()
    console.print(ui.chooser_table([_device("First"), _device("Second")], 1))
    drawn = console.export_text()
    marked = [line for line in drawn.splitlines() if ui.SELECTED_MARK in line]
    assert len(marked) == 1
    assert "Second" in marked[0]


def test_the_chooser_says_it_is_looking_before_it_has_found_anything() -> None:
    # An empty table with headings would read as "no hubs exist" rather than
    # as "still listening".
    console = _terminal()
    console.print(ui.chooser_table([], 0))
    assert "Looking for hubs" in console.export_text()


def test_the_connected_table_stars_the_default_hub() -> None:
    console = _terminal()
    first, second = _hub("First"), _hub("Second")
    console.print(ui.connected_table([first, second], second.id))
    rows = {
        name: line
        for line in console.export_text().splitlines()
        for name in ("First", "Second")
        if name in line
    }
    assert "*" in rows["Second"], "the default hub is not marked"
    assert "*" not in rows["First"], "a hub that is not the default is marked"


def test_rows_are_a_table_on_a_terminal() -> None:
    # Aligned for reading rather than tab-separated for parsing.
    console = _terminal()
    ui.rows(console, [("name", "Pybricks Hub")])
    drawn = console.export_text()
    assert "\t" not in drawn
    assert "name" in drawn
    assert "Pybricks Hub" in drawn


# ------------------------------------------------------------------ pieces


def test_a_spinner_is_only_a_spinner_on_a_terminal() -> None:
    # In a log file it would be a few hundred escape codes and no information.
    piped, buffer = _piped()
    with ui.spinner(piped, "Working..."):
        pass
    assert buffer.getvalue() == ""


@pytest.mark.parametrize("err", [False, True])
def test_the_error_console_is_the_one_that_writes_to_stderr(err: bool) -> None:
    assert ui.console(err=err).stderr is err


@pytest.mark.parametrize("err", [False, True])
def test_a_version_in_a_message_is_drawn_in_one_piece(err: bool) -> None:
    # Rich's default highlighter reads a string as a Python repr, so a firmware
    # version arrived as "4.1" in cyan, then ".0b3-" plain, then "134" in cyan
    # again -- one thing drawn as three, which reads as though the tool were
    # confused about what it was saying. Messages are sentences, so nothing in
    # them is coloured unless we said to colour it.
    sentence = "Connected to Hub (City hub, firmware 4.1.0b3-134-gb4bd99edd)."
    drawn = [
        segment
        for segment in ui.console(err=err).render(sentence)
        if segment.text.strip()
    ]
    assert [segment.text for segment in drawn] == [sentence]
    assert not any(segment.style for segment in drawn)
