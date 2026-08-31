# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What a session makes of what the hub told it.

How a program ended has to be read out of what it printed, because nothing in
the protocol says -- see ``TRACEBACK`` in :mod:`pb_cli.session`. That reading is
the most delicate rule in the tool and, now that it is a function of a value,
each case is a line here rather than a whole session driving a fake hub.

The end-to-end tests still run these cases through a real command and a fake
screen, which is what proves the wiring. These prove the rule.
"""

from __future__ import annotations

import pb_client
from pb_cli.session import TAIL_BYTES, TRACEBACK, Running

CRASH = TRACEBACK + b'\r\n  File "__main__.py", line 3\r\nZeroDivisionError: /\r\n'
"""A program that ended by raising something nothing caught."""

INTERRUPT = TRACEBACK + b'\r\n  File "__main__.py", line 3\r\nKeyboardInterrupt: \r\n'
"""The same shape, but the exception is the user pressing Ctrl-C."""


def status(*, running: bool) -> pb_client.Status:
    """Build a hub status report saying whether something is running.

    Args:
        running: What the hub should be reporting.

    Returns:
        A report carrying nothing else of interest.
    """
    return pb_client.Status(
        seq=0,
        flags=0,
        names=(),
        program_running=running,
        running_program_id=None,
        selected_slot=None,
    )


def test_a_program_that_just_ends_did_not_crash() -> None:
    program = Running("program", watch_for_crash=True).saw(b"done\r\n")
    assert not program.crashed
    assert not program.interrupted


def test_a_traceback_at_the_end_is_a_crash() -> None:
    program = Running("program", watch_for_crash=True).saw(CRASH)
    assert program.crashed
    assert not program.interrupted


def test_ctrl_c_is_not_a_crash() -> None:
    # It is a traceback like any other, so which exception ended it is the only
    # thing telling "the user stopped it" from "it failed".
    program = Running("program", watch_for_crash=True).saw(INTERRUPT)
    assert program.interrupted
    assert not program.crashed


def test_a_traceback_that_scrolled_away_is_not_a_crash() -> None:
    # Printed, caught, and then a long way of output afterwards: by the time
    # the program ends it is out of the tail, which is the point of a tail.
    program = (
        Running("program", watch_for_crash=True)
        .saw(CRASH)
        .saw(b"carrying on\r\n")
        .saw(b"x" * TAIL_BYTES)
    )
    assert not program.crashed


def test_the_tail_stays_bounded() -> None:
    program = Running("program", watch_for_crash=True)
    for _ in range(10):
        program = program.saw(b"y" * TAIL_BYTES)
    assert len(program.tail) == TAIL_BYTES


def test_the_interpreter_is_not_watched_for_crashes() -> None:
    # An exception at a prompt is an ordinary thing to type, so it must not
    # fail the command -- and nothing is kept, so there is nothing to read.
    repl = Running("interpreter").saw(CRASH)
    assert repl.tail == b""
    assert not repl.crashed
    assert not repl.interrupted


def test_a_status_saying_something_runs_means_it_arrived() -> None:
    program = Running("program").note(status(running=True))
    assert program.arrived
    assert program.running
    assert not program.gone


def test_nothing_running_before_it_arrived_is_not_the_end() -> None:
    # The moment between asking the hub to start something and the hub saying
    # it has. Treating that as the end would close the session immediately.
    program = Running("program").note(status(running=False))
    assert not program.gone


def test_nothing_running_before_it_arrived_leaves_it_running() -> None:
    # A real race, found by running the tests under coverage, which is slow
    # enough to lose it: leaving in this window skipped stopping a program the
    # command had started, and a hub with something running does not
    # advertise, so it could not then be found.
    program = Running("program").we_started_it().note(status(running=False))
    assert program.running
    assert program.ours


def test_stopping_after_it_arrived_is_the_end() -> None:
    program = Running("program").note(status(running=True)).note(status(running=False))
    assert program.gone
    assert not program.running


def test_it_stays_ended_once_it_has_ended() -> None:
    program = (
        Running("program")
        .note(status(running=True))
        .note(status(running=False))
        .note(status(running=False))
    )
    assert program.gone


def test_starting_it_makes_it_ours_to_stop() -> None:
    assert not Running("program").ours
    assert Running("program").we_started_it().ours


def test_taking_something_in_leaves_the_value_alone() -> None:
    # The reason this is a value: a session hands the same one to four tasks,
    # and none of them can change what another is looking at.
    before = Running("program", watch_for_crash=True)
    before.saw(CRASH)
    before.note(status(running=True))
    assert before.tail == b""
    assert not before.arrived
