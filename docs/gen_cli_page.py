# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Generate the command reference from the command line itself.

Run by mkdocs-gen-files at build time. The alternative is writing the same
thing twice -- once for ``pb --help`` and once for the site -- and then
noticing months later that only one of them was updated. cyclopts already
knows every command, its arguments, its help and the order they are grouped
in, so it is asked.

Two things are done to what it returns, both about reading order, and both
because it walks the app in the order commands were registered -- which is the
order the functions happen to be defined in and the order the modules happen to
be imported, neither of which means anything to a reader.
"""

import re
from itertools import pairwise
from pathlib import Path

import mkdocs_gen_files

# Importing the CLI module is what registers the commands, exactly as it is
# for the real entry point.
import pb.cli  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pb.app import app

PAGE = Path("commands.md")

TITLE = """<!-- Generated from the pb command line by docs/gen_cli_page.py. -->

# Commands

"""

TOC_HEADING = "## Table of Contents"

HEADING = re.compile(r"^## pb (?P<command>[a-z][a-z -]*)$")
"""The heading that opens one command's section."""

LISTED = re.compile(r"^\* \[`(?P<command>[a-z-]+)`\]\(#pb-[a-z-]+\):")
"""One command in the group listings, which are already in the right order.

Read from the generated page rather than from the app's own internals: the
listings under **Hubs**, **Programs** and **Background service** are what
``pb --help`` shows, in the order it shows them, so they are a supported
answer to a question the library offers no other way to ask.
"""


def without_contents(markdown: str) -> str:
    """Drop the generated table of contents.

    Args:
        markdown: The reference as cyclopts wrote it.

    Returns:
        The same, without its own contents list. The theme builds one from the
        headings and puts it beside the page, so a second copy in the body is
        one more thing to scroll past -- and it is the copy that stays nested
        when the headings are flat.
    """
    lines = markdown.splitlines(keepends=True)
    try:
        start = lines.index(TOC_HEADING + "\n")
    except ValueError:
        # cyclopts stopped writing one, or writes it differently. Leaving it
        # in is a much smaller problem than failing the build over it.
        return markdown
    # Only the contents list itself. The group listings -- **Hubs**, and the
    # rest -- follow it under the same heading, and they are the overview
    # worth keeping: they are what `pb --help` shows.
    end = next(
        (n for n in range(start + 1, len(lines)) if lines[n].startswith(("## ", "**"))),
        len(lines),
    )
    return "".join(lines[:start] + lines[end:])


def in_help_order(markdown: str) -> str:
    """Put the command sections in the order the help puts them.

    Args:
        markdown: The reference as cyclopts wrote it.

    Returns:
        The same sections, reordered. ``pb --help`` lists commands by what
        somebody has to do first, which is a decision worth making once; a
        reference in a different order makes the reader wonder which of the
        two is deliberate.
    """
    lines = markdown.splitlines(keepends=True)
    starts = [n for n, line in enumerate(lines) if HEADING.match(line.rstrip("\n"))]
    if not starts:
        return markdown

    preamble = "".join(lines[: starts[0]])
    bounds = [*starts, len(lines)]
    sections = [
        (
            HEADING.match(lines[first].rstrip("\n")).group("command"),  # pyright: ignore[reportOptionalMemberAccess]
            "".join(lines[first:last]),
        )
        for first, last in pairwise(bounds)
    ]

    wanted = [
        match.group("command")
        for line in preamble.splitlines()
        if (match := LISTED.match(line))
    ]
    if not wanted:
        # The listings moved or changed shape. An order nobody chose is
        # better than no page at all.
        return markdown

    def rank(named: tuple[str, str]) -> int:
        # A subcommand takes its parent's place, so `pb service log` stays
        # with `pb service` instead of leading the page on its own.
        top = named[0].split()[0]
        return wanted.index(top) if top in wanted else len(wanted)

    # Stable, so subcommands keep the order their own group gave them.
    return preamble + "".join(text for _, text in sorted(sections, key=rank))


with mkdocs_gen_files.open(PAGE, "w") as fd:
    fd.write(TITLE)
    # Every command at the same heading level, so `pb service start` sits
    # beside `pb run` in the sidebar rather than a layer down. It is one
    # command, and it is typed as one; making a reader assemble its name from
    # two headings before they can look it up serves nobody.
    written = app.generate_docs(heading_level=2, flatten_commands=True)
    fd.write(in_help_order(without_contents(written)))

mkdocs_gen_files.set_edit_path(PAGE, "docs/gen_cli_page.py")
