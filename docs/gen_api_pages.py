# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Generate one API reference page per documented object, plus the nav.

Run by mkdocs-gen-files at build time. Generating rather than hand-listing
means a newly added module, class or method cannot be silently left
undocumented.

One page per object rather than one per module. A module page carries the
module's own docstring and links to what is in it; a class page carries the
class's and links to its members; a method, a function, a property or a
constant gets a page of its own. That makes every object addressable -- a link
to a method goes to a page about that method rather than to a heading part way
down a long one -- and keeps each page short enough to read.

The object tree is read with griffe, which is the same library mkdocstrings
renders from, so this cannot disagree with what ends up on the page.
"""

from __future__ import annotations

from pathlib import Path

import griffe
import mkdocs_gen_files
from mkdocs_gen_files.nav import Nav

PACKAGES = {
    "pb-core": "pb_core",
    "pb-client": "pb_client",
    "pbd": "pbd",
    "pb": "pb",
}

ROOT = Path(__file__).parent.parent

SEARCH_PATHS = [ROOT / "packages" / dist / "src" for dist in PACKAGES]

GROUPS = (
    ("module", "Modules", "Modules"),
    ("class", "Classes", "Classes"),
    ("function", "Functions", "Methods"),
    ("attribute", "Attributes", "Attributes"),
)
"""How a parent page lists what is under it: kind, then what to call it.

Two names because the same kind reads differently depending on where it is
found -- a function on a class is a method, and calling it anything else on
the page a reader looks it up on is just wrong. The order is the way further
in first, then the things somebody calls, then the values they pass; within a
section, alphabetical, because a reference is looked things up in rather than
read from the top.
"""

CONSTRUCTOR = "__init__"
"""The one dunder that does not get a page.

Its parameters are how the class is built, so they belong on the class's own
page, which is where ``merge_init_into_class`` puts them. A page of its own
would be a nav entry called ``__init__`` under every class, saying what the
page above it already says.
"""

nav = Nav()

PAGES: set[str] = set()
"""Every object that will get a page, by dotted path.

Filled before anything is written, because a page lists what is under it as
links and a link can only be made to something that will exist. A name
re-exported from a private module -- ``AnyioListener``, whose home is
``pb_core.ipc.backend._anyio_listener`` -- has nowhere to point, so it is named
without a link rather than left as a cross-reference that cannot resolve.
"""

_CHILDREN: dict[str, list[tuple[str, griffe.Object | griffe.Alias]]] = {}
"""What each object holds, worked out once.

The same question gets asked repeatedly -- deciding whether a module is worth a
page means asking what is in it, and writing the page asks again -- and the
answer cannot change during a build.
"""


def is_documented(name: str) -> bool:
    """Say whether a member is part of the documented surface.

    Args:
        name: The member's name.

    Returns:
        True unless the name is private. A dunder such as ``__version__``
        counts as public, because it is something callers read; ``__init__``
        and its like are functions and are covered by the class they are on.
    """
    if name == CONSTRUCTOR:
        return False
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


def summarise(obj: griffe.Object | griffe.Alias) -> str:
    """Take the one-line summary out of an object's docstring.

    Args:
        obj: The object to describe.

    Returns:
        Its docstring's first line, or an empty string. Google-style
        docstrings open with a one-line summary, which is exactly what a list
        of links wants beside each entry.
    """
    try:
        docstring = obj.docstring
    except (griffe.AliasResolutionError, KeyError):
        return ""
    if docstring is None:
        return ""
    first = docstring.value.strip().splitlines()
    return first[0].strip() if first else ""


def target_of(member: griffe.Object | griffe.Alias) -> str:
    """Return the path a link to ``member`` should point at.

    Args:
        member: The member being listed on its parent's page.

    Returns:
        Where the object is really documented. A name re-exported by a package
        -- ``pb_client.Daemon`` for ``pb_client.daemon.Daemon`` -- is
        documented once, at the place it is defined, and listed as a link to
        there. Documenting it twice would give two pages claiming the same
        identifier, which is exactly what a cross-reference cannot resolve.
    """
    if isinstance(member, griffe.Alias):
        return str(member.target_path)
    return member.canonical_path


def has_something_to_say(member: griffe.Object | griffe.Alias) -> bool:
    """Say whether an object is worth a page of its own.

    Args:
        member: The object to judge.

    Returns:
        True if it carries a docstring, or is a module or class holding
        something that does. An object with nothing written about it would
        make a page showing a name and a type, which is worse than not being
        listed: mkdocstrings leaves undocumented members out for the same
        reason, and generating a page each would quietly overrule that.
    """
    if summarise(member):
        return True
    if isinstance(member, griffe.Module | griffe.Class):
        return any(True for _ in children(member))
    return False


def children(obj: griffe.Object) -> list[tuple[str, griffe.Object | griffe.Alias]]:
    """List what belongs on ``obj``'s page, in the order it is shown.

    Args:
        obj: A module or a class.

    Returns:
        Its documented public members, grouped by kind in :data:`GROUPS` order
        and sorted by name within each group.
    """
    cached = _CHILDREN.get(obj.canonical_path)
    if cached is not None:
        return cached

    order = {kind: n for n, (kind, *_) in enumerate(GROUPS)}
    members = [
        (name, member)
        for name, member in obj.members.items()
        if is_documented(name)
        and member.kind.value in order
        and has_something_to_say(member)
    ]
    listed = sorted(members, key=lambda pair: (order[pair[1].kind.value], pair[0]))
    _CHILDREN[obj.canonical_path] = listed
    return listed


def collect(obj: griffe.Object) -> None:
    """Record that ``obj``, and everything under it, will have a page.

    Args:
        obj: A module or a class.
    """
    PAGES.add(obj.canonical_path)
    for _, member in children(obj):
        if isinstance(member, griffe.Alias):
            continue
        if isinstance(member, griffe.Module | griffe.Class):
            collect(member)
        else:
            PAGES.add(member.canonical_path)


def slug(name: str, taken: set[str]) -> str:
    """Turn a member's name into a file name nothing else in the folder uses.

    Args:
        name: The member's name.
        taken: The lower-cased names already used in this folder, added to.

    Returns:
        A file name. Compared case-insensitively because Windows and macOS
        file systems are: a class ``Choice`` and a function ``choice`` in one
        module would otherwise quietly become the same page.
    """
    candidate = name
    while candidate.lower() in taken:
        candidate += "_"
    taken.add(candidate.lower())
    return candidate


def stanza(path: str, *, leaf: bool) -> str:
    """Write the mkdocstrings block that renders one object.

    Args:
        path: The object's dotted path.
        leaf: Whether the object has no page of its own beneath it.

    Returns:
        The block. A parent renders without its members, since each of them
        has a page; the links to those pages are written separately.
    """
    block = f"::: {path}\n    options:\n      heading_level: 1\n"
    if not leaf:
        block += "      members: false\n"
    return block


def write_page(
    obj: griffe.Object,
    parts: tuple[str, ...],
    directory: Path,
) -> None:
    """Write the page for one object, and the pages for everything under it.

    Args:
        obj: The module or class to document.
        parts: Its dotted path, as nav wants it.
        directory: Where its children's pages go, relative to ``reference/``.
    """
    members = children(obj)
    page = directory / "index.md" if members else directory.with_suffix(".md")

    on_a_class = isinstance(obj, griffe.Class)
    lines = [stanza(obj.canonical_path, leaf=not members)]
    for kind, in_a_module, in_a_class in GROUPS:
        heading = in_a_class if on_a_class else in_a_module
        listed = [(name, m) for name, m in members if m.kind.value == kind]
        if not listed:
            continue
        lines.append(f"\n## {heading}\n\n")
        for name, member in listed:
            described = summarise(member)
            tail = f" -- {described}" if described else ""
            target = target_of(member)
            link = f"[`{name}`][{target}]" if target in PAGES else f"`{name}`"
            lines.append(f"- {link}{tail}\n")

    emit(page, "".join(lines), obj)
    nav[parts] = page.as_posix()

    taken: set[str] = {"index"}
    for name, member in members:
        if isinstance(member, griffe.Alias):
            continue  # documented where it is defined
        child = directory / slug(name, taken)
        if isinstance(member, griffe.Module | griffe.Class):
            write_page(member, (*parts, name), child)
        else:
            leaf = child.with_suffix(".md")
            emit(leaf, stanza(member.canonical_path, leaf=True), member)
            nav[(*parts, name)] = leaf.as_posix()


def emit(page: Path, text: str, obj: griffe.Object | griffe.Alias) -> None:
    """Write one generated page, pointing "edit this page" at the source.

    Args:
        page: Where it goes, relative to ``reference/``.
        text: The page's contents.
        obj: What it documents, for the source location.
    """
    with mkdocs_gen_files.open(Path("reference") / page, "w") as fd:
        fd.write(text)
    try:
        filepath = obj.filepath
    except (griffe.AliasResolutionError, KeyError):
        return
    if isinstance(filepath, list) or not filepath.is_relative_to(ROOT):
        return
    mkdocs_gen_files.set_edit_path(Path("reference") / page, filepath.relative_to(ROOT))


loaded = {
    module: griffe.load(module, search_paths=SEARCH_PATHS, allow_inspection=False)
    for module in PACKAGES.values()
}

for package in loaded.values():
    collect(package)

# The section's own page, written by hand in docs/reference/index.md and
# listed first so that it is what the section opens on.
nav["API reference"] = "index.md"

for module, package in loaded.items():
    write_page(package, (module,), Path(module))

with mkdocs_gen_files.open("reference/SUMMARY.md", "w") as fd:
    fd.writelines(nav.build_literate_nav())
