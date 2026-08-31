# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The library's docstrings, checked for things that do not render.

These are the only docstrings in the repository that are published, so they are
the only ones where a mistake in the markup is read by somebody who does not
have the source. Sphinx role syntax is the mistake that actually happened: the
front page of the reference said ":func:`anyio.fail_after`" where it meant a
link, in those words, because mkdocstrings renders Markdown and knows nothing
about roles.

The build catches the opposite mistake on its own -- a cross-reference that
resolves to nothing fails `properdocs build --strict` -- so between the two,
what is published is checked in both directions.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "pb_client"

ROLE = re.compile(r":[a-z]+:`[^`]+`")
"""Sphinx's way of writing a cross-reference, which renders here as itself.

The Markdown equivalent is ``[text][identifier]``, or ``[`identifier`][]`` to
use the identifier as the text. Both resolve against the inventories in
``properdocs.yml``, so a reference to CPython or to anyio becomes a link out.
"""


def modules() -> list[Path]:
    """List the library's source files."""
    return sorted(SOURCE.rglob("*.py"))


def test_there_is_something_to_check() -> None:
    # Without this, the test below passes perfectly for a moved package.
    assert len(modules()) >= 5


@pytest.mark.parametrize("module", modules(), ids=lambda path: path.name)
def test_no_sphinx_roles_in_published_docstrings(module: Path) -> None:
    found = ROLE.findall(module.read_text(encoding="utf-8"))
    assert not found, (
        f"{module.name} uses Sphinx role syntax {found}, which the published "
        f"reference shows as itself. Write [text][identifier] instead."
    )
