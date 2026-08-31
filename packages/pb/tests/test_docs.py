# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The hand-written parts of the documentation, checked against the code.

The command reference is generated from the app, so it cannot drift. The
landing page is written by hand and can, which is not a reason to generate it:
its table says "Send a script without running it" where the terminal says
"Compile a script and send it to a hub without running it", and the shorter
one is better where somebody is deciding whether they care.

So the wording is left alone and the two things that go quietly wrong are
checked: a row naming a command that no longer exists, and a command nobody
remembered to mention.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import pb.cli  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pb.app import app

INDEX = Path(__file__).resolve().parents[3] / "docs" / "index.md"

ROW = re.compile(r"^\| `(pb [a-z ]+?)(?: [A-Z-]+)?` \| (.+?) \|$")
"""One row of the table: a command, perhaps with an argument, and a sentence.

The argument is dropped -- `pb run SCRIPT` is `pb run` -- because what is
being looked up is the command.
"""


def _page() -> str:
    return INDEX.read_text(encoding="utf-8")


def _rows() -> list[str]:
    """Read the commands named by the table on the landing page."""
    return [
        match.group(1)
        for line in _page().splitlines()
        if (match := ROW.match(line.strip()))
    ]


def _top_level() -> list[str]:
    """Name every command `pb` offers, ignoring their subcommands."""
    return [
        name
        for name in app
        if not name.startswith("-")  # --help and --version are not commands
    ]


def test_the_landing_page_has_a_table_at_all() -> None:
    # Without this, the test below passes perfectly for an empty table.
    assert len(_rows()) >= 10


@pytest.mark.parametrize("command", _rows())
def test_every_row_names_a_command_that_exists(command: str) -> None:
    found = app
    for word in command.split()[1:]:
        assert word in found, f"docs/index.md names `{command}`, which does not exist"
        found = found[word]


@pytest.mark.parametrize("command", _top_level())
def test_every_command_is_mentioned(command: str) -> None:
    # The drift that actually happened: two commands were added and the
    # landing page had to be remembered separately, twice.
    assert f"pb {command}" in _page(), (
        f"`pb {command}` exists but docs/index.md never mentions it"
    )
