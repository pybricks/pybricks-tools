# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The hub the user chose, remembered between runs of the service."""

from __future__ import annotations

from pathlib import Path

import pytest

from pbd.hub.selection import Selection


def test_a_choice_outlives_the_thing_that_made_it(tmp_path: Path) -> None:
    # The whole point: the service can restart, and the machine with it,
    # without the user having to say again which hub they are working with.
    Selection(tmp_path / "selection.json").choose("Spare Hub")
    assert Selection(tmp_path / "selection.json").name == "Spare Hub"


def test_nothing_chosen_reads_as_nothing_chosen(tmp_path: Path) -> None:
    assert Selection(tmp_path / "selection.json").name is None


def test_a_choice_can_be_forgotten(tmp_path: Path) -> None:
    selection = Selection(tmp_path / "selection.json")
    selection.choose("Spare Hub")
    selection.choose(None)
    assert selection.name is None
    assert Selection(tmp_path / "selection.json").name is None


def test_a_file_that_makes_no_sense_is_ignored(tmp_path: Path) -> None:
    # A preference that cannot be read is a preference that was never set.
    # Refusing to start, or raising into whatever command happened to be
    # running, would be a wildly disproportionate answer to a damaged file.
    path = tmp_path / "selection.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert Selection(path).name is None

    # And the next choice repairs it rather than inheriting the mess.
    Selection(path).choose("Spare Hub")
    assert Selection(path).name == "Spare Hub"


@pytest.mark.parametrize("stored", ["[]", '{"hub": 7}', '{"hub": ""}', "null"])
def test_a_name_that_is_not_a_name_is_ignored(tmp_path: Path, stored: str) -> None:
    path = tmp_path / "selection.json"
    path.write_text(stored, encoding="utf-8")
    assert Selection(path).name is None


def test_a_choice_that_cannot_be_stored_still_applies(tmp_path: Path) -> None:
    # The directory is a file, so writing into it cannot work. The user asked
    # about a hub rather than about a file, so the choice holds for as long as
    # this service runs and the failure goes to the log.
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    selection = Selection(blocked / "selection.json")
    selection.choose("Spare Hub")
    assert selection.name == "Spare Hub"


def test_the_file_says_what_it_holds(tmp_path: Path) -> None:
    # It sits in the user's state directory where somebody will eventually
    # open it, so it is worth being readable.
    path = tmp_path / "selection.json"
    Selection(path).choose("Spare Hub")
    assert path.read_text(encoding="utf-8") == '{\n  "hub": "Spare Hub"\n}\n'
