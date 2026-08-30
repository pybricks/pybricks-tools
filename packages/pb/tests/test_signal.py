# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""How a signal strength is drawn."""

from __future__ import annotations

import re

import pytest

from pb import ui

MARKUP = re.compile(r"\[[^\]]*\]")


def _drawn(rssi: int | None) -> str:
    """The bar without its colours, which is what occupies the column."""
    return MARKUP.sub("", ui.signal_bars(rssi))


@pytest.mark.parametrize("rssi", [None, -120, -100, -85, -75, -60, -40, -20])
def test_every_bar_is_the_same_width(rssi: int | None) -> None:
    # The bars share a column with each other, and a row that is one character
    # narrower than the rest makes the whole table look broken.
    assert len(_drawn(rssi)) == len(_drawn(-50))


def _lit(rssi: int | None) -> int:
    """How many of the four positions are drawn as bars rather than as dots."""
    return sum(1 for char in _drawn(rssi) if char in ui.SIGNAL_BLOCKS)


def test_a_stronger_signal_is_never_fewer_bars() -> None:
    # The one property a signal meter has to have, and it has to hold in the
    # characters themselves rather than only in their colour: a reader who
    # cannot tell green from red, or a terminal with styling stripped, still
    # has to be able to see which hub is nearer.
    lit = [_lit(rssi) for rssi in range(-120, -19)]
    assert lit == sorted(lit)
    assert lit[0] < lit[-1], "the meter never moves"


def test_a_hub_that_was_heard_always_gets_a_bar() -> None:
    # Zero bars would read as "not there", and it is: it just advertised.
    assert ui.SIGNAL_BLOCKS[0] in ui.signal_bars(-120)


def test_a_hub_with_no_measurement_is_not_drawn_as_a_weak_one() -> None:
    # Some transports report no strength at all. Showing one bar would be a
    # lie about a hub that might be right here.
    assert ui.SIGNAL_BLOCKS[0] not in ui.signal_bars(None)


def test_the_strongest_signal_fills_the_bar() -> None:
    assert _drawn(-20) == ui.SIGNAL_BLOCKS
