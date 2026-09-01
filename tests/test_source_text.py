# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What the source files are allowed to contain, as bytes.

A repository that talks to a terminal ends up with a lot of control bytes in
it -- escape sequences a hub sends, keystrokes a test types -- and they must be
written as escape sequences rather than as the byte itself. This is the test
that says so, because the mistake is invisible in an editor: VS Code renders a
real Escape byte as a small badge and everything looks fine, while `git diff`,
a review in a browser, `grep` output and any terminal that acts on the byte
rather than printing it all disagree about what is there.

It has happened, which is why this exists. Two test files were written through
a shell heredoc, which ate a level of backslash escaping, so `b"\\x1b[A"`
became a literal Escape followed by `[A`. The tests passed either way -- the
bytes are the same at runtime -- so nothing but reading the file would have
found it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CONTROL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
"""Every byte below a space that a text file has no business holding literally.

Tab, newline and carriage return are left out: the first is whitespace a file
may legitimately be indented with, and the other two are how lines end. What
remains is the set that has no meaning in a source file except as data, and
data belongs in an escape sequence.
"""


def sources() -> list[Path]:
    """List the Python files git is tracking.

    Asked of git rather than globbed, so that a virtual environment, a build
    directory or anything else ignored cannot fail this.

    Returns:
        The paths, relative to the repository root.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [Path(name) for name in listed.stdout.decode().split("\0") if name]


def test_there_is_something_to_check() -> None:
    # Without this, the test below passes perfectly for a git that said
    # nothing at all.
    assert len(sources()) >= 50


@pytest.mark.parametrize("path", sources(), ids=str)
def test_control_bytes_are_written_as_escape_sequences(path: Path) -> None:
    raw = (ROOT / path).read_bytes()
    found = {byte[0] for byte in CONTROL.findall(raw)}
    assert not found, (
        f"{path} holds control bytes literally: "
        + ", ".join(f"0x{byte:02x}" for byte in sorted(found))
        + ". Write them as escape sequences, so that what the file says is "
        "the same in an editor, a diff and a terminal."
    )
