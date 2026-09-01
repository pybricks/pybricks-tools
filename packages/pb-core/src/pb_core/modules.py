# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Finding the modules a program is made of.

A program sent to a hub is a run of compiled modules in one blob, and this is
the part that works out which modules those are. It is deliberately separate
from compiling them: discovering the set is pure work on paths and syntax
trees, needs no compiler, and is where every interesting decision lives.

**The hub has no notion of a package.** Its import hook does a single
``strcmp`` against the fully qualified name, so ``robot.motors`` is simply a
module whose name contains a dot, and an ``__init__.py`` means nothing to
either side. That is why this does not use :mod:`modulefinder`, which walks
compiled bytecode and raises on an implicit namespace package -- the tool this
replaces turns that into advice to add an ``__init__.py``, which is advice to
do something with no effect. A dotted name maps to a path by replacing the dots
with slashes, and that is the whole of the resolution rule.

**Modules are looked for in more than one directory.** A project that keeps
its source in ``src`` resolves ``robot.motors`` against ``src``, and the entry
script's own directory is searched as well -- as Python puts it on ``sys.path``
-- so a helper beside a loose script is found even when that script sits
outside the project's source tree. :func:`pb_core.project.module_roots` is what
works the list out.

**Anything that does not resolve is assumed to be built into the firmware.**
``pybricks.hubs``, ``umath`` and the rest are on the hub already, so a name
this cannot find is not an error; it is the normal case for most of what a
program imports.

**Relative imports do not work on the hub at all.** ``pb_builtin_import``
raises ``NotImplementedError`` for any import with a level above zero, so a
leading dot is refused here instead, where the file and line can be named.

One firmware limitation worth knowing but not worth guarding against: because
the lookup is flat, ``import robot.motors`` binds the ``robot.motors`` module
to the name ``robot``, so a later ``robot.motors.spin()`` fails on the
attribute. ``from robot.motors import spin`` and ``import robot.motors as
motors`` both work. Bare dotted imports are rare enough that refusing them
would cost more than it saved.
"""

from __future__ import annotations

import ast
import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NamedTuple

import anyio

__all__ = [
    "MAIN_MODULE",
    "PRECOMPILED_SUFFIX",
    "SOURCE_SUFFIX",
    "Module",
    "RelativeImportError",
    "candidates",
    "canonical",
    "discover",
    "entry_name",
    "find_imports",
    "resolve",
]

logger = logging.getLogger(__name__)

MAIN_MODULE: Final = "__main__"
"""What a hub older than Pybricks profile v1.5.0 requires the program to be called.

Firmware from that profile onwards runs whichever module comes first in the
blob and takes the program's displayed name from it, so the entry module is
named after its file instead. Before it, the module is found by this name, so
it has to be this name.
"""

SOURCE_SUFFIX: Final = ".py"
"""What a module to be compiled is called."""

PRECOMPILED_SUFFIX: Final = ".mpy"
"""What an already-compiled module is called.

Looked for after the source, so a ``.py`` beside a stale ``.mpy`` wins. It is
the only way to use a module built for a native architecture, which cannot be
produced from source here.
"""


class RelativeImportError(Exception):
    """A program used an import the hub cannot perform."""

    def __init__(self, lineno: int, path: Path | None = None) -> None:
        """Record where the import is.

        Args:
            lineno: The line the import is on.
            path: The file it is in, when known.
        """
        where = f"{path}:{lineno}" if path is not None else f"line {lineno}"
        # The location goes last, not first. A message beginning with a path
        # gets its first letter capitalised on the way to the user, which
        # turns rel.py into Rel.py.
        super().__init__(f"the hub cannot do relative imports ({where})")
        self.lineno = lineno
        self.path = path


class Module(NamedTuple):
    """One module of a program."""

    name: str
    """What the hub will call it, and what an import of it must say."""

    path: Path
    """The file it came from."""

    precompiled: bool
    """Whether the file is already compiled and passes through untouched."""


def find_imports(source: str, *, path: Path | None = None) -> tuple[str, ...]:
    """List the modules a script imports, in the order it imports them.

    Args:
        source: The Python source.
        path: The file it came from, used only to say where a bad import is.

    Returns:
        Every fully qualified name the script imports, without duplicates.
        Only the names as written: the hub looks up a dotted name whole, so
        ``import a.b`` needs ``a.b`` and not ``a`` as well.

    Raises:
        RelativeImportError: If the script imports relative to its own package,
            which the hub refuses.
    """

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Not this function's business. The compiler is about to be handed the
        # same source and says exactly which line and what is wrong with it,
        # which is a far better message than anything that could be built
        # here -- so say nothing and let it.
        logger.debug("could not parse %s to find its imports", path or "<string>")
        return ()

    names: dict[str, None] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.name] = None
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise RelativeImportError(node.lineno, path)

            if node.module is not None:
                names[node.module] = None

    return tuple(names)


def candidates(name: str, root: Path) -> tuple[Path, ...]:
    """List the files a module name could refer to, in the order to try them.

    Args:
        name: A fully qualified module name, such as ``robot.motors``.
        root: The directory names are relative to.

    Returns:
        The source file first, then the precompiled one. Nothing is touched to
        work this out, which is what makes the naming rule testable on its own.
    """
    base = root.joinpath(*name.split("."))
    # Built by hand rather than with with_suffix, which would treat a dot in
    # the last component as a suffix to replace.
    return tuple(
        base.parent / f"{base.name}{suffix}"
        for suffix in (SOURCE_SUFFIX, PRECOMPILED_SUFFIX)
    )


async def resolve(name: str, roots: Sequence[Path]) -> Path | None:
    """Find the file a module name refers to.

    Args:
        name: A fully qualified module name, such as ``robot.motors``.
        roots: The directories names are relative to, in the order to try
            them.

    Returns:
        The file, or ``None`` if there is none -- which means the module is
        expected to be part of the firmware.
    """

    for root in roots:
        for candidate in candidates(name, root):
            if await anyio.Path(candidate).is_file():
                return candidate

    return None


def canonical(path: Path) -> Path:
    """Put a path in a form two spellings of one file agree on.

    Deliberately not ``Path.resolve``, which reads the filesystem to follow
    symbolic links -- so it would block the event loop, and it would also merge
    two names for one file that the hub would treat as two modules.

    Args:
        path: Any path.

    Returns:
        It, made absolute, with ``.`` and ``..`` folded away.
    """
    # absolute() rather than resolve(): it only prepends the working
    # directory, where resolve() reads the filesystem. normpath then folds the
    # dots away, which pathlib has no equivalent of.
    return Path(os.path.normpath(path.absolute()))


def entry_name(entry: Path, roots: Sequence[Path]) -> str:
    """Name the module a program starts from.

    Args:
        entry: The script the program starts at.
        roots: The directories module names are looked for in.

    Returns:
        The dotted name the entry script would be imported by. Where more than
        one root contains it -- ``src`` and ``src/robot`` both contain
        ``src/robot/demo.py`` -- the **most qualified** name wins, so this is
        ``robot.demo`` rather than ``demo``. That is deliberately not the
        resolution order, which puts the script's own directory first the way
        Python does: the fully qualified name is the one another module would
        have to write to import the program, and it is the more informative
        thing for the hub to display. A script under none of the roots keeps
        its stem, since there is no dotted name for it and nothing can import
        it anyway.
    """
    where = canonical(entry)
    best = entry.stem
    parts = 0

    for root in roots:
        try:
            relative = where.relative_to(canonical(root))
        except ValueError:
            continue

        pieces = (*relative.parent.parts, relative.stem)
        if len(pieces) > parts:
            best = ".".join(pieces)
            parts = len(pieces)

    return best


async def _read(path: Path) -> str:
    """Read a source file off the event loop.

    Args:
        path: The file.

    Returns:
        Its text.
    """
    return await anyio.Path(path).read_text(encoding="utf-8")


async def discover(entry: Path, roots: Sequence[Path]) -> tuple[Module, ...]:
    """Work out every module a program needs.

    Args:
        entry: The script the program starts at.
        roots: The directories module names are relative to, in the order to
            try them.

    Returns:
        The modules, **entry first**. Position is what the firmware uses to
        pick the program to run, so the order is part of the answer rather
        than an incidental detail.

    Raises:
        RelativeImportError: If any module imports relative to its package.
        OSError: If a module that resolved cannot be read.
    """
    found = [Module(entry_name(entry, roots), entry, precompiled=False)]
    # The entry's own file, so that a module importing the program by name is
    # not packed a second time. On current firmware both copies would carry
    # the same name and the import would re-execute the program.
    seen_paths = {canonical(entry)}
    seen_names = {found[0].name}

    pending = list(find_imports(await _read(entry), path=entry))

    while pending:
        name = pending.pop(0)
        if name in seen_names:
            continue

        seen_names.add(name)

        path = await resolve(name, roots)
        if path is None:
            logger.debug("%s is not a local module; assuming the hub has it", name)
            continue

        if canonical(path) in seen_paths:
            logger.debug("%s is the program itself; not packing it twice", name)
            continue

        seen_paths.add(canonical(path))

        precompiled = path.suffix == PRECOMPILED_SUFFIX
        found.append(Module(name, path, precompiled=precompiled))

        if not precompiled:
            # A compiled module cannot be read for its imports, so it is a
            # leaf: whatever it needs has to be packed by naming it somewhere
            # that can be read.
            pending.extend(find_imports(await _read(path), path=path))

    logger.info(
        "program is %d module(s): %s",
        len(found),
        ", ".join(module.name for module in found),
    )
    return tuple(found)
