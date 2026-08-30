# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What ``pb status`` chooses to say."""

from __future__ import annotations

from pb import ui
from pbd.hub.protocol import StatusFlag


def test_a_battery_warning_is_said_in_plain_words() -> None:
    assert ui.hub_warnings(["BATTERY_LOW_VOLTAGE_WARNING"]) == ["battery low"]


def test_ordinary_state_is_not_a_warning() -> None:
    # Advertising, connected and running are what a working hub looks like.
    quiet = ["BLE_ADVERTISING", "BLE_HOST_CONNECTED", "USER_PROGRAM_RUNNING"]
    assert ui.hub_warnings(quiet) == []


def test_two_flags_that_mean_the_same_thing_are_said_once() -> None:
    # A hub on its way down sets both, and "switching off, switching off"
    # would read like a fault in the tool.
    both = ["SHUTDOWN_REQUEST", "SHUTDOWN"]
    assert ui.hub_warnings(both) == ["switching off"]


def test_an_unknown_flag_is_ignored() -> None:
    # Newer firmware may report flags this version has never heard of, and
    # they must not become a crash or a line of gibberish.
    assert ui.hub_warnings(["SOMETHING_NEW"]) == []


def test_every_warning_names_a_real_status_flag() -> None:
    # The wording is keyed on the wire name because the CLI does not import
    # the hub protocol. This is what stops the two drifting apart: a renamed
    # or removed flag silently stops warning anybody otherwise.
    #
    # Importing pbd is fine here and only here: pb depends on the daemon so
    # that installing the CLI alone can start one, and never imports it.
    known = {flag.name for flag in StatusFlag}
    assert set(ui.HUB_WARNINGS) <= known
