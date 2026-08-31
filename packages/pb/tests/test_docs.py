# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The hand-written parts of the documentation, checked against the code.

The command reference is generated from the app, so it lists every command and
cannot drift. The landing page is written by hand and is deliberately not a
list of everything: it shows the few commands a day is actually made of and
sends the reader to the reference for the rest.

That makes "a command nobody remembered to mention" a non-problem -- the
generated page mentions it -- and leaves one thing that still goes quietly
wrong, which is a row naming a command that has been renamed or removed. The
wording is nobody's business but the writer's: the table says "Send a script
without running it" where the terminal says "Compile a script and send it to a
hub without running it", and the shorter one is better where somebody is
deciding whether they care.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import pb.cli  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pb.app import app

INDEX = Path(__file__).resolve().parents[3] / "docs" / "index.md"

ROW = re.compile(r"^\| `pb ([^`]+)` \| (.+?) \|$")
"""One row of the table: how a command is typed, and a sentence about it."""


def _page() -> str:
    return INDEX.read_text(encoding="utf-8")


def _words(spelling: str) -> str:
    """Reduce how a command is typed to the command itself.

    Args:
        spelling: What the row shows, such as ``run -c CODE``.

    Returns:
        Only the words that name a command -- ``run`` -- with options and
        the capitalised placeholders for arguments dropped, because what is
        being looked up is the command.
    """
    named = [
        word
        for word in spelling.split()
        if not word.startswith("-") and not word.isupper()
    ]
    return " ".join(named)


def _rows() -> list[str]:
    """Read the commands named by the table on the landing page."""
    return [
        _words(match.group(1))
        for line in _page().splitlines()
        if (match := ROW.match(line.strip()))
    ]


def test_the_landing_page_has_a_table_at_all() -> None:
    # Without this, the test below passes perfectly for an empty table.
    assert len(_rows()) >= 3


@pytest.mark.parametrize("command", _rows())
def test_every_row_names_a_command_that_exists(command: str) -> None:
    found = app
    for word in command.split():
        assert word in found, (
            f"docs/index.md names `pb {command}`, which does not exist"
        )
        found = found[word]
