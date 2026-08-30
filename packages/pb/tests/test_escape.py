# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Telling keys meant for the hub from keys meant for us."""

from __future__ import annotations

import pytest

from pb import escape
from pb.escape import Action, Filter

MICROPYTHON_CONTROL_KEYS = bytes(
    [
        0x01,  # Ctrl-A, raw REPL and start of line
        0x02,  # Ctrl-B, friendly REPL and back one character
        0x03,  # Ctrl-C, interrupt
        0x04,  # Ctrl-D, soft reset and end of input
        0x05,  # Ctrl-E, paste mode and end of line
        0x06,  # Ctrl-F, raw paste and forward one character
        0x0B,  # Ctrl-K, kill to end of line
        0x0E,  # Ctrl-N, next in history
        0x10,  # Ctrl-P, previous in history
        0x15,  # Ctrl-U, kill the line
        0x17,  # Ctrl-W, kill the word
    ]
)
"""Every control key MicroPython's REPL and line editor claim.

Taken from ``readline.c`` and ``pyexec.c`` in the firmware's MicroPython. The
escape key must not be any of these, and none of them may be intercepted: each
one is the only way to do something at the hub's prompt.
"""


def test_ordinary_typing_goes_straight_to_the_hub() -> None:
    for_hub, actions = Filter().feed(b"print(1)\r")
    assert for_hub == b"print(1)\r"
    assert actions == []


@pytest.mark.parametrize("key", MICROPYTHON_CONTROL_KEYS)
def test_micropython_control_keys_are_never_intercepted(key: int) -> None:
    # Every one of these means something at the hub's prompt, so taking any of
    # them for ourselves would remove the only way to do that thing.
    assert key != escape.ESCAPE_KEY
    for_hub, actions = Filter().feed(bytes([key]))
    assert for_hub == bytes([key])
    assert actions == []


def test_the_escape_key_is_not_passed_on() -> None:
    for_hub, actions = Filter().feed(bytes([escape.ESCAPE_KEY, ord("q")]))
    assert for_hub == b""
    assert actions == [Action.QUIT]


def test_a_command_split_across_two_reads_still_works() -> None:
    # The usual case rather than an edge case: a console hands over one key at
    # a time, so the escape and its command almost never arrive together.
    keys = Filter()
    assert keys.feed(bytes([escape.ESCAPE_KEY])) == (b"", [])
    assert keys.armed
    assert keys.feed(b"q") == (b"", [Action.QUIT])
    assert not keys.armed


def test_pressing_the_escape_key_twice_sends_it_to_the_hub() -> None:
    for_hub, actions = Filter().feed(bytes([escape.ESCAPE_KEY, escape.ESCAPE_KEY]))
    assert for_hub == bytes([escape.ESCAPE_KEY])
    assert actions == []


def test_a_key_that_is_not_a_command_is_reported_rather_than_eaten() -> None:
    # Silently swallowing it would leave the user wondering where their
    # keystroke went; saying so lets the session offer the help.
    for_hub, actions = Filter().feed(bytes([escape.ESCAPE_KEY, ord("z")]))
    assert for_hub == b""
    assert actions == [Action.UNKNOWN]


def test_typing_around_a_command_is_kept_in_order() -> None:
    for_hub, actions = Filter().feed(
        b"ab" + bytes([escape.ESCAPE_KEY, ord("s")]) + b"cd"
    )
    assert for_hub == b"abcd"
    assert actions == [Action.STOP]


def test_several_commands_in_one_read_all_arrive() -> None:
    keys = bytes([escape.ESCAPE_KEY, ord("s"), escape.ESCAPE_KEY, ord("q")])
    assert Filter().feed(keys) == (b"", [Action.STOP, Action.QUIT])


def test_an_arrow_key_reaches_the_hub_whole() -> None:
    # Both platforms deliver this as the same three bytes, and the hub's line
    # editor needs all three.
    assert Filter().feed(b"\x1b[A") == (b"\x1b[A", [])


def test_the_escape_key_can_be_changed() -> None:
    keys = Filter(key=0x1C)
    assert keys.feed(bytes([escape.ESCAPE_KEY])) == (bytes([escape.ESCAPE_KEY]), [])
    assert keys.feed(bytes([0x1C, ord("q")])) == (b"", [Action.QUIT])


def test_control_bytes_are_named_the_way_people_say_them() -> None:
    assert escape.describe(0x1D) == "Ctrl-]"
    assert escape.describe(0x03) == "Ctrl-C"


def test_the_help_spells_out_what_to_type() -> None:
    lines = escape.help_lines()
    assert ("Ctrl-] q", "leave the session") in lines
    # Including how to send the escape key itself, which is otherwise the one
    # key a user cannot work out.
    assert ("Ctrl-] Ctrl-]", "send Ctrl-] to the hub") in lines
