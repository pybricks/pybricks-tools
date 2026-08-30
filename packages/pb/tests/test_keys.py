# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Turning what a terminal sends into what a key means.

Reading is per platform; deciding is not, and deciding is where the traps are.
These drive the decision directly with a scripted stream of characters, so the
Windows grammar and the terminal grammar are both covered wherever the tests
happen to run.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pb import keys
from pb.keys import Key


def _typed(monkeypatch: pytest.MonkeyPatch, *chars: str) -> None:
    """Queue up characters for the next reads, then nothing."""
    waiting: Iterator[str] = iter(chars)
    monkeypatch.setattr(keys, "read_ready", lambda: next(waiting, None))


def test_nothing_typed_is_not_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The chooser polls constantly, so this is the answer nearly every time.
    _typed(monkeypatch)
    assert keys.poll_key() is None


@pytest.mark.parametrize("char", ["\r", "\n"])
def test_enter_arrives_either_way(monkeypatch: pytest.MonkeyPatch, char: str) -> None:
    _typed(monkeypatch, char)
    assert keys.poll_key() is Key.ENTER


@pytest.mark.parametrize("prefix", ["\x00", "\xe0"])
def test_the_windows_arrows(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    # The console sends a prefix and then a scan code, and uses either prefix
    # depending on the key.
    _typed(monkeypatch, prefix, "H")
    assert keys.poll_key() is Key.UP
    _typed(monkeypatch, prefix, "P")
    assert keys.poll_key() is Key.DOWN


def test_the_terminal_arrows(monkeypatch: pytest.MonkeyPatch) -> None:
    _typed(monkeypatch, "\x1b", "[", "A")
    assert keys.poll_key() is Key.UP
    _typed(monkeypatch, "\x1b", "[", "B")
    assert keys.poll_key() is Key.DOWN


def test_escape_on_its_own_means_leave(monkeypatch: pytest.MonkeyPatch) -> None:
    # The trap: escape both stands alone and opens the arrow sequences. What
    # tells them apart is that nothing follows a bare escape.
    _typed(monkeypatch, "\x1b")
    assert keys.poll_key() is Key.CANCEL


def test_an_unknown_escape_sequence_is_not_mistaken_for_an_arrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Page Up, a function key, a mouse report: plenty arrives that this does
    # not know, and none of it should move the selection.
    _typed(monkeypatch, "\x1b", "[", "5", "~")
    assert keys.poll_key() is Key.OTHER


def test_an_unknown_windows_scan_code_is_ignored_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _typed(monkeypatch, "\x00", "K")
    assert keys.poll_key() is Key.OTHER


@pytest.mark.parametrize("char", ["q", "Q"])
def test_q_leaves(monkeypatch: pytest.MonkeyPatch, char: str) -> None:
    _typed(monkeypatch, char)
    assert keys.poll_key() is Key.CANCEL


def test_an_ordinary_letter_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _typed(monkeypatch, "x")
    assert keys.poll_key() is Key.OTHER


def test_keys_cannot_be_read_when_input_is_not_a_terminal() -> None:
    # pytest captures stdin, so this is the piped case. It is the guard that
    # stops a chooser waiting forever for a keystroke that cannot come.
    assert keys.keys_available() is False
