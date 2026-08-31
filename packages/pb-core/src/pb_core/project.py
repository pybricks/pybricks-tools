# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Where a project keeps the source a program imports.

A script inside a package cannot be run with its own directory as the import
root: ``src/robot/demo.py`` importing ``robot.motors`` would look for
``src/robot/robot/motors.py``, which is the wrong directory and always will be,
because the directory a script sits in is not the directory its own package
name is relative to.

There is no option for this, because a project that keeps its code in a
subdirectory has already said so in its project file. There is also **no
standard key** to read, and that was worth checking rather than assuming: the
``[project]`` specification defines twenty keys, none of which concerns
directory structure, and says that source and package directory configuration
is explicitly left to the build backend. So what follows is a table of
conventions rather than an implementation of a standard, and it has to be
maintained as one.

The saving grace is that the conventions agree. ``src/`` is the documented
default for ``uv_build``, auto-detected by setuptools, and written out
explicitly by the rest -- so the derivation is the table, and then the
convention, which every row of the table agrees with:

1. an explicit key this recognises, or
2. ``<project>/src``, if it exists, or
3. the project directory itself.

``[project] import-names`` from PEP 794 is the key to prefer once projects
carry it: standardised in October 2025, backend-independent, and enough to
locate the directory by looking for each name at the project root and under a
source directory. It is optional, specified with SHOULD, and almost nothing in
the wild has it yet, so it is not something to depend on -- but it is what
eventually replaces most of this module.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, cast

import anyio

from pb_core import modules

__all__ = [
    "PROJECT_FILE",
    "SOURCE_DIR",
    "find_project",
    "module_roots",
    "source_root",
]

logger = logging.getLogger(__name__)

PROJECT_FILE: Final = "pyproject.toml"
"""What a Python project is described by."""

SOURCE_DIR: Final = "src"
"""The directory to assume when nothing says otherwise.

The documented default for ``uv_build``, what setuptools auto-detects, and the
convention every other backend spells out by hand.
"""


def _table(document: Mapping[str, Any], *keys: str) -> Mapping[str, Any] | None:
    """Follow a path of nested tables.

    Args:
        document: The parsed document.
        keys: The table names to descend through.

    Returns:
        The table, or ``None`` if any step is missing or is not a table.
    """
    current: Mapping[str, Any] = document
    for key in keys:
        value = current.get(key)
        if not isinstance(value, dict):
            return None
        current = cast("dict[str, Any]", value)
    return current


def _string(value: object) -> str | None:
    """Take a value only if it is a string.

    Args:
        value: Anything.

    Returns:
        The string, or ``None``.
    """
    return value if isinstance(value, str) else None


def _first_string(value: object) -> str | None:
    """Take the first entry of a list of strings.

    A project declaring several source directories gets the first one. One flat
    namespace is all the hub has -- two roots could each hold a ``motors.py``,
    and there would be no way to say which the program meant -- so merging them
    would promise something that cannot be delivered.

    Args:
        value: Anything, hopefully a list.

    Returns:
        The first string in it, or ``None``.
    """
    if isinstance(value, list):
        for item in cast("list[object]", value):
            found = _string(item)
            if found is not None:
                return found
    return None


def _declared_root(document: Mapping[str, Any]) -> str | None:
    """Read the source directory a build backend has been told to use.

    Each entry was read at that backend's own documentation. Returning the
    empty string is meaningful and different from returning ``None``: it is how
    ``uv_build`` says the modules are at the top level.

    Args:
        document: The parsed project file.

    Returns:
        The directory, relative to the project, or ``None`` if nothing said.
    """
    # uv_build: module-root, which defaults to "src".
    uv = _table(document, "tool", "uv", "build-backend")
    if uv is not None and (found := _string(uv.get("module-root"))) is not None:
        return found

    # setuptools: either package-dir's root entry or where the finder looks.
    setuptools = _table(document, "tool", "setuptools")
    if setuptools is not None:
        package_dir: object = setuptools.get("package-dir")
        if isinstance(package_dir, dict):
            found = _string(cast("dict[str, object]", package_dir).get(""))
            if found is not None:
                return found
    find = _table(document, "tool", "setuptools", "packages", "find")
    if find is not None and (found := _first_string(find.get("where"))) is not None:
        return found

    # hatchling. sources remaps a directory away, and packages names the
    # package rather than the root -- packages = ["src/foo"] means a root of
    # "src" -- which is the one row here that is easy to read wrongly.
    wheel = _table(document, "tool", "hatch", "build", "targets", "wheel")
    if wheel is not None:
        sources: object = wheel.get("sources")
        if isinstance(sources, dict):
            for key in cast("dict[str, object]", sources):
                return key
        if (found := _first_string(sources)) is not None:
            return found
        for key in ("packages", "only-include"):
            if (found := _first_string(wheel.get(key))) is not None:
                parent = str(Path(found).parent)
                return "" if parent == "." else parent

    # poetry: packages = [{include = "foo", from = "src"}]
    poetry = _table(document, "tool", "poetry")
    if poetry is not None:
        packages: object = poetry.get("packages")
        if isinstance(packages, list):
            for item in cast("list[object]", packages):
                if isinstance(item, dict):
                    entry = cast("dict[str, object]", item)
                    if (found := _string(entry.get("from"))) is not None:
                        return found

    # pdm-backend and maturin.
    pdm = _table(document, "tool", "pdm", "build")
    if pdm is not None and (found := _string(pdm.get("package-dir"))) is not None:
        return found
    maturin = _table(document, "tool", "maturin")
    if (
        maturin is not None
        and (found := _string(maturin.get("python-source"))) is not None
    ):
        return found

    return None


async def _read_project(path: Path) -> Mapping[str, Any] | None:
    """Parse a project file, if it describes a project at all.

    A ``pyproject.toml`` only counts if it has a ``[project]`` table.
    Otherwise a configuration-only file -- a virtual workspace root, for one --
    would capture a scratch script anywhere beneath it and resolve that
    script's imports against a package it has nothing to do with.

    Args:
        path: The file.

    Returns:
        The parsed document, or ``None`` if it cannot be read, is not valid
        TOML, or has no ``[project]`` table.
    """
    try:
        text = await anyio.Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        # The ordinary case while walking up a tree, and not worth a line in
        # the log: "could not read C:\\pyproject.toml" reads like a problem.
        return None
    except OSError:
        logger.debug("could not read %s", path)
        return None
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # Not this tool's file to complain about. Somebody else's broken
        # pyproject.toml is not a reason to refuse to run a script.
        logger.debug("%s is not valid TOML", path)
        return None
    if not isinstance(document.get("project"), dict):
        return None
    return document


async def find_project(start: Path | None = None) -> Path | None:
    """Find the project a directory belongs to.

    Args:
        start: Where to look from, defaulting to the working directory.

    Returns:
        The nearest ``pyproject.toml`` at or above ``start`` that has a
        ``[project]`` table, or ``None``. Searching past a file that says
        nothing is what lets a project inside a monorepo be found rather than
        shadowed by whatever sits above it.
    """
    here = (start or Path.cwd()).absolute()
    for directory in (here, *here.parents):
        candidate = directory / PROJECT_FILE
        if await _read_project(candidate) is not None:
            return candidate
    return None


async def source_root(project_file: Path) -> Path:
    """Work out where a project keeps the source its programs import.

    Args:
        project_file: The project's ``pyproject.toml``.

    Returns:
        The directory module names are relative to.
    """
    project = project_file.parent
    document = await _read_project(project_file)
    declared = _declared_root(document) if document is not None else None
    if declared is not None:
        # An explicit empty string means the top level, which is how uv_build
        # spells a flat layout.
        return project / declared if declared else project
    if await anyio.Path(project / SOURCE_DIR).is_dir():
        return project / SOURCE_DIR
    return project


async def module_roots(
    entry: Path, *, directory: Path | None = None
) -> tuple[Path, ...]:
    """Work out what a program's imports are looked for in.

    Args:
        entry: The script the program starts at.
        directory: What ``-C`` named, if it was given.

    Returns:
        The directories to try, in order, and never empty.

        **The entry script's own directory comes first**, the way Python puts a
        script's directory at the front of ``sys.path``. Without it a project
        with a ``src`` layout swallows any script kept outside that tree: a
        helper module sitting beside the script would not be found, and the
        program would compile happily and then fail on the hub with an
        ImportError. Found by running the command rather than by a test.

        Then the project's source root, by these rules:

        | Given | The next root is |
        |---|---|
        | a directory with a project file at or above it | derived from that file |
        | a directory with none | the directory itself |
        | no directory, but a project file at or above the working directory | derived from that file |
        | neither | nothing; only the script's own directory applies |

        Which root gives the entry module its *name* is a separate question --
        see :func:`pb_core.modules.entry_name`, which takes the most qualified
        name any of them yields.
    """
    # The entry script's own directory first, the way Python puts a script's
    # directory at the front of sys.path -- so a bare "import helpers" beside a
    # script finds that script's neighbour rather than a file of the same name
    # at the top of the project.
    roots: list[Path] = [entry.parent]

    project = await find_project(directory)
    if project is not None:
        roots.append(await source_root(project))
        logger.debug("import roots %s, from %s", roots, project)
    elif directory is not None:
        roots.append(directory)

    seen: list[Path] = []
    for root in roots:
        if not any(modules.canonical(root) == modules.canonical(kept) for kept in seen):
            seen.append(root)
    return tuple(seen)
