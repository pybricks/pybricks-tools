# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Telling keys meant for the hub from keys meant for us.

The filter is a value, so each of these says what one read means and nothing
about when it happened: feed bytes to a filter, and check what comes back and
which filter the next read would use.
"""

from __future__ import annotations

import pytest

from pb_cli import escape
from pb_cli.escape import Action, Filter

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


def sorted_by(keys: Filter, data: bytes) -> tuple[bytes, tuple[Action, ...]]:
    """Feed one read to a filter, for a test that does not care what is next.

    Args:
        keys: The filter to read with.
        data: What was typed.

    Returns:
        What goes to the hub, and what was asked of ``pb``.
    """
    _, for_hub, actions = keys.feed(data)
    return for_hub, actions


def test_ordinary_typing_goes_straight_to_the_hub() -> None:
    assert sorted_by(Filter(), b"print(1)\r") == (b"print(1)\r", ())


@pytest.mark.parametrize("key", MICROPYTHON_CONTROL_KEYS)
def test_micropython_control_keys_are_never_intercepted(key: int) -> None:
    # Every one of these means something at the hub's prompt, so taking any of
    # them for ourselves would remove the only way to do that thing.
    assert key != escape.ESCAPE_KEY
    assert sorted_by(Filter(), bytes([key])) == (bytes([key]), ())


def test_the_escape_key_is_not_passed_on() -> None:
    typed = bytes([escape.ESCAPE_KEY, ord("q")])
    assert sorted_by(Filter(), typed) == (b"", (Action.QUIT,))


def test_a_command_split_across_two_reads_still_works() -> None:
    # The usual case rather than an edge case: a console hands over one key at
    # a time, so the escape and its command almost never arrive together. The
    # filter handed back by the first read is what carries the half-typed
    # command into the second.
    half, for_hub, actions = Filter().feed(bytes([escape.ESCAPE_KEY]))
    assert (for_hub, actions) == (b"", ())
    assert half.armed

    rest, for_hub, actions = half.feed(b"q")
    assert (for_hub, actions) == (b"", (Action.QUIT,))
    assert not rest.armed


def test_feeding_a_filter_leaves_it_alone() -> None:
    # The point of a value: the filter that read the escape key still reads it
    # the same way afterwards, so nothing can be quietly carried between two
    # sessions that happen to share one.
    keys = Filter()
    keys.feed(bytes([escape.ESCAPE_KEY]))
    assert not keys.armed
    assert sorted_by(keys, b"q") == (b"q", ())


def test_pressing_the_escape_key_twice_sends_it_to_the_hub() -> None:
    typed = bytes([escape.ESCAPE_KEY, escape.ESCAPE_KEY])
    assert sorted_by(Filter(), typed) == (bytes([escape.ESCAPE_KEY]), ())


def test_a_key_that_is_not_a_command_is_reported_rather_than_eaten() -> None:
    # Silently swallowing it would leave the user wondering where their
    # keystroke went; saying so lets the session offer the help.
    typed = bytes([escape.ESCAPE_KEY, ord("z")])
    assert sorted_by(Filter(), typed) == (b"", (Action.UNKNOWN,))


def test_typing_around_a_command_is_kept_in_order() -> None:
    typed = b"ab" + bytes([escape.ESCAPE_KEY, ord("s")]) + b"cd"
    assert sorted_by(Filter(), typed) == (b"abcd", (Action.STOP,))


def test_several_commands_in_one_read_all_arrive() -> None:
    typed = bytes([escape.ESCAPE_KEY, ord("s"), escape.ESCAPE_KEY, ord("q")])
    assert sorted_by(Filter(), typed) == (b"", (Action.STOP, Action.QUIT))


def test_an_arrow_key_reaches_the_hub_whole() -> None:
    # Both platforms deliver this as the same three bytes, and the hub's line
    # editor needs all three.
    assert sorted_by(Filter(), b"\x1b[A") == (b"\x1b[A", ())


def test_the_escape_key_can_be_changed() -> None:
    keys = Filter(key=0x1C)
    ours = bytes([escape.ESCAPE_KEY])
    assert sorted_by(keys, ours) == (ours, ())
    assert sorted_by(keys, bytes([0x1C, ord("q")])) == (b"", (Action.QUIT,))


def test_control_bytes_are_named_the_way_people_say_them() -> None:
    assert escape.describe(0x1D) == "Ctrl-]"
    assert escape.describe(0x03) == "Ctrl-C"


def test_escape_is_called_escape_rather_than_the_byte_it_encodes() -> None:
    # It is Ctrl-[ as far as the encoding goes, and calling it that in the
    # status line would leave the panic key looking like something else.
    assert escape.describe(escape.STOP_KEY) == "Esc"


def test_the_help_spells_out_what_to_type() -> None:
    lines = escape.help_lines()
    assert ("Ctrl-] q", "leave the session") in lines
    # Including how to send the escape key itself, which is otherwise the one
    # key a user cannot work out.
    assert ("Ctrl-] Ctrl-]", "send Ctrl-] to the hub") in lines
    # Help is advertised as the key a hand can reach without a shift.
    assert ("Ctrl-] h", "show this help") in lines


def test_the_help_puts_stopping_first() -> None:
    # Somebody reading this list in a hurry is reading it for one line, and
    # having to find it under three others is the whole problem with Ctrl-] s.
    keystrokes, what = escape.help_lines()[0]
    assert keystrokes == "Esc Esc"
    assert "stop" in what


def test_help_answers_to_the_key_the_other_tools_use_as_well() -> None:
    # h is what is advertised, but ? is what a user of screen or telnet will
    # try first, and refusing it would be a needless surprise.
    for key in (b"h", b"H", b"?"):
        typed = bytes([escape.ESCAPE_KEY]) + key
        assert sorted_by(Filter(), typed) == (b"", (Action.HELP,))


# --------------------------------------------------------- Escape, twice over


def test_two_escapes_stop_the_hub() -> None:
    # The whole point: one key, pressed twice, and nothing else has to be
    # remembered at the moment somebody wants their robot to stop.
    typed = bytes([escape.STOP_KEY, escape.STOP_KEY])
    assert sorted_by(Filter(), typed) == (b"", (Action.STOP,))


def test_two_escapes_split_across_reads_still_stop_the_hub() -> None:
    # The ordinary case, not an edge one: a console hands over one key at a
    # time, so pressing a key twice arrives as two reads nearly always.
    half, for_hub, actions = Filter().feed(bytes([escape.STOP_KEY]))
    assert (for_hub, actions) == (b"", ())
    assert half.held

    rest, for_hub, actions = half.feed(bytes([escape.STOP_KEY]))
    assert (for_hub, actions) == (b"", (Action.STOP,))
    assert not rest.held


def test_one_escape_reaches_the_hub_when_the_next_key_arrives() -> None:
    # Held rather than dropped, and held rather than sent: which it was is not
    # known until something follows it, and nothing needs it sooner.
    half, for_hub, actions = Filter().feed(bytes([escape.STOP_KEY]))
    assert (for_hub, actions) == (b"", ())
    assert sorted_by(half, b"a") == (bytes([escape.STOP_KEY]) + b"a", ())


@pytest.mark.parametrize(
    "sequence",
    [
        b"[A",  # up
        b"[B",  # down
        b"[C",  # right
        b"[D",  # left
        b"OA",  # up, from a terminal in application cursor mode
        b"[3~",  # delete
        b"[1;5C",  # Ctrl-right, which carries a modifier
    ],
    ids=lambda seq: seq.decode("latin-1").replace("", "ESC "),
)
def test_no_escape_sequence_is_mistaken_for_a_stop(sequence: bytes) -> None:
    # This is why it is two presses and not one. Every sequence a terminal
    # sends is Escape then something that is not Escape, so two in a row can
    # be recognised with no timing window at all -- and a window is the thing
    # that would be wrong occasionally rather than never.
    assert sorted_by(Filter(), sequence) == (sequence, ())


@pytest.mark.parametrize(
    "sequence",
    [b"[A", b"OB", b"[1;5C"],
    ids=lambda seq: seq.decode("latin-1").replace("", "ESC "),
)
def test_a_sequence_arriving_a_byte_at_a_time_still_reaches_the_hub(
    sequence: bytes,
) -> None:
    # A slow link, or a console handing over one key at a time. The bytes must
    # come out in order and none of them may be read as a command.
    keys = Filter()
    seen = bytearray()

    for byte in sequence:
        keys, for_hub, actions = keys.feed(bytes([byte]))
        seen += for_hub
        assert actions == ()

    assert bytes(seen) == sequence


def test_an_escape_before_a_command_is_passed_on_rather_than_swallowed() -> None:
    # The held escape is re-read as if it had never been held, so the key
    # after it can perfectly well be the escape key starting a command.
    typed = bytes([escape.STOP_KEY, escape.ESCAPE_KEY, ord("q")])
    assert sorted_by(Filter(), typed) == (bytes([escape.STOP_KEY]), (Action.QUIT,))


def test_an_escape_after_the_escape_key_is_a_command_and_not_a_stop() -> None:
    # Ctrl-] claims the key that follows it whatever that key is, so this is
    # an unknown command rather than half of a stop.
    typed = bytes([escape.ESCAPE_KEY, escape.STOP_KEY])
    assert sorted_by(Filter(), typed) == (b"", (Action.UNKNOWN,))


def test_three_escapes_stop_once_and_hold_the_third() -> None:
    rest, for_hub, actions = Filter().feed(bytes([escape.STOP_KEY] * 3))
    assert (for_hub, actions) == (b"", (Action.STOP,))
    assert rest.held


def test_four_escapes_stop_twice() -> None:
    # Pairs, rather than "any two escapes seen since the last one", so a hand
    # that hammers the key gets what it asked for and nothing is left over.
    rest, for_hub, actions = Filter().feed(bytes([escape.STOP_KEY] * 4))
    assert (for_hub, actions) == (b"", (Action.STOP, Action.STOP))
    assert not rest.held


def test_typing_around_a_stop_is_kept_in_order() -> None:
    typed = b"ab" + bytes([escape.STOP_KEY, escape.STOP_KEY]) + b"cd"
    assert sorted_by(Filter(), typed) == (b"abcd", (Action.STOP,))
