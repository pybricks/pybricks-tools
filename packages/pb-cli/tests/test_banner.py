# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The status line along the bottom of a session."""

from __future__ import annotations

import pb_client
from pb_cli import banner
from pb_cli.banner import Banner

HUB = pb_client.HubInfo(
    id="fake:1",
    name="Rover",
    state="connected",
    hub_type="City hub",
    firmware="4.1.0b3",
    protocol="1.6.0",
    address="00:00:00:00:00:01",
    num_slots=0,
    max_program_size=16224,
    subscribers=1,
    program_running=False,
    status_names=(),
)


def _status(
    *names: str, running: bool = True, program: int | None = 128
) -> pb_client.Status:
    return pb_client.Status(
        seq=1,
        flags=0,
        names=names,
        program_running=running,
        running_program_id=program,
        selected_slot=None,
    )


class _Screen:
    """A console that only remembers what was written to it."""

    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        self.written = bytearray()
        self._size = (columns, rows)

    def read(self) -> bytes:
        return b""

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def size(self) -> tuple[int, int]:
        return self._size

    def resize(self, columns: int, rows: int) -> None:
        self._size = (columns, rows)

    @property
    def text(self) -> str:
        return self.written.decode("utf-8", "replace")


def test_the_bottom_row_is_reserved_and_given_back() -> None:
    screen = _Screen(rows=24)
    with Banner(screen, HUB):
        # Every row but the last scrolls, so the hub's output can never write
        # over the status line.
        assert "\x1b[1;23r" in screen.text
        assert "\x1b[24;1H" in screen.text
    # A margin left set is as broken as a terminal left in raw mode, and just
    # as invisible.
    assert screen.text.endswith("\x1b[r\x1b[24;1H\x1b[2K")


def test_the_line_names_the_hub_and_what_it_is_doing() -> None:
    screen = _Screen()
    with Banner(screen, HUB) as bar:
        bar.update(_status())
        assert "Rover" in screen.text
        assert "City hub" in screen.text
        assert "REPL" in screen.text


def test_a_program_that_is_not_the_repl_is_named_differently() -> None:
    assert banner.describe_running(_status(program=0)) == "program running"
    assert banner.describe_running(_status(running=False)) == "nothing running"
    assert banner.describe_running(None) == "waiting for the hub"


def test_the_worst_warning_is_the_one_shown() -> None:
    # One line, so one warning: an empty battery matters more than a low one.
    assert (
        banner.worst_warning(
            _status("BATTERY_LOW_VOLTAGE_WARNING", "BATTERY_LOW_VOLTAGE_SHUTDOWN")
        )
        == "battery empty"
    )
    assert banner.worst_warning(_status("BLE_HOST_CONNECTED")) is None


def test_a_battery_warning_reaches_the_line() -> None:
    screen = _Screen()
    with Banner(screen, HUB) as bar:
        bar.update(_status("BATTERY_LOW_VOLTAGE_WARNING"))
        assert "battery low" in screen.text


def test_an_unchanged_status_is_not_redrawn() -> None:
    # The hub reports its status about twice a second whether or not anything
    # has changed, and redrawing on each would flicker for no reason.
    screen = _Screen()
    with Banner(screen, HUB) as bar:
        bar.update(_status())
        before = len(screen.written)
        bar.update(_status())
        assert len(screen.written) == before


def test_the_line_never_fills_the_last_cell() -> None:
    # Writing the bottom-right cell scrolls some terminals, which would take
    # the status line with it.
    line = banner._Layout("left", "right").fit(40)
    assert len(line) == 39


def test_a_long_name_pushes_the_help_off_rather_than_wrapping() -> None:
    line = banner._Layout("x" * 100, "Ctrl-] h for help").fit(40)
    assert len(line) == 39
    assert "Ctrl-]" not in line


def test_resizing_moves_the_line_to_the_new_bottom_row() -> None:
    screen = _Screen(rows=24)
    with Banner(screen, HUB) as bar:
        screen.resize(80, 40)
        bar.tick()
        assert "\x1b[1;39r" in screen.text
        assert "\x1b[40;1H" in screen.text


def test_a_notice_appears_and_then_goes_away(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    clock = [1000.0]
    monkeypatch.setattr(banner.time, "monotonic", lambda: clock[0])
    screen = _Screen()
    with Banner(screen, HUB) as bar:
        bar.notice("the hub is not reading its input")
        assert "not reading" in screen.text
        clock[0] += banner.NOTICE_SECONDS + 1
        bar.tick()
        assert "not reading" not in screen.text.rsplit("\x1b[2K", 1)[-1]


def test_a_terminal_too_short_for_a_status_line_still_works() -> None:
    screen = _Screen(rows=1)
    with Banner(screen, HUB) as bar:
        bar.update(_status())
        bar.write(b"output")
    assert b"output" in screen.written
