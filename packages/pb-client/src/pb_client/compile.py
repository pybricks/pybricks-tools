# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Compiling Python for a hub.

Compilation happens on the client rather than in the service, for two reasons.
The script is on the caller's filesystem, so the service would otherwise need to
read files belonging to whoever asked -- which stops being reasonable the moment
the service is on another machine. And it keeps the compiler, which is a native
binary, out of the service entirely.

The compiler is a subprocess, so it runs in a worker thread rather than on the
event loop.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import anyio
import anyio.to_thread
import mpy_cross_v6

from pb_client.errors import PbClientError
from pb_core.modules import MAIN_MODULE, Module, RelativeImportError, discover

logger = logging.getLogger(__name__)

__all__ = [
    "MAIN_MODULE",
    "MPY_ABI_VERSION",
    "SOURCE_NAME",
    "CompileError",
    "CompiledModule",
    "CompiledProgram",
    "compile_program",
    "compile_script",
    "compile_source",
    "compiler_version",
    "describe_compiler",
    "pack_program",
]

MPY_ABI_VERSION = 6
"""The ``.mpy`` format this compiler produces, and the hub must accept.

The byte after the magic in every ``.mpy`` file. It is checked on a module
that arrives already compiled, because the firmware checks it too and refuses
the whole program -- so it is much better said here, naming the file.
"""


class CompileError(PbClientError):
    """The program could not be compiled."""

    def __init__(self, message: str, output: str = "") -> None:
        """Record the compiler's own diagnostics.

        Args:
            message: Short description.
            output: The compiler's stderr, which names the offending line.
        """
        super().__init__(message)
        self.output = output


class CompiledModule(NamedTuple):
    """One compiled module of a program."""

    name: str
    """What the hub will call it."""

    mpy: bytes
    """Its compiled form."""


@dataclass(frozen=True, slots=True)
class CompiledProgram:
    """A compiled program, not yet in the form the hub stores.

    Compiling and packing are separate because the entry module's *name*
    depends on the hub, and the hub is not known when compiling starts.
    Nothing else does: the compiler is told the file's name, for tracebacks,
    and never the module's. So a program can be compiled before anything is
    connected to -- which keeps a syntax error a local failure, reported
    without a hub having to be found first -- and named once the hub has said
    which profile it speaks.
    """

    modules: tuple[CompiledModule, ...]
    """Every module, entry first.

    Position is what current firmware uses to choose the program to run, so
    the order is part of the value rather than an incidental detail.
    """

    def pack(self, *, legacy_main: bool = False) -> bytes:
        """Build the bytes to store on the hub.

        Args:
            legacy_main: Whether the hub needs the entry module to be called
                ``__main__``. True for Pybricks profiles before v1.5.0, which
                look the entry module up by that name; from v1.5.0 the first
                module is run whatever it is called, and its name is what the
                hub shows as the program's.

        Returns:
            The packed program.
        """
        modules = self.modules

        if legacy_main:
            modules = (modules[0]._replace(name=MAIN_MODULE), *modules[1:])

        packed = pack_program([(module.name, module.mpy) for module in modules])
        logger.info(
            "packed %d module(s) into %d bytes, starting at %s",
            len(modules),
            len(packed),
            modules[0].name,
        )
        return packed


def describe_compiler() -> str:
    """Return the compiler path and its architecture, for the startup log.

    Worth logging because the published Windows ARM64 wheel currently contains
    an x86-64 binary, so on that platform the compiler runs under emulation.
    Recording it turns a puzzling failure into an obvious one.

    Returns:
        A short description of the compiler in use.
    """
    path = Path(mpy_cross_v6.MPY_CROSS_PATH)
    machine = "unknown"

    try:
        header = path.read_bytes()[:0x400]
        if header[:2] == b"MZ":
            offset = int.from_bytes(header[0x3C:0x40], "little")
            value = int.from_bytes(header[offset + 4 : offset + 6], "little")
            machine = {0x8664: "x86-64", 0xAA64: "arm64", 0x14C: "x86"}.get(
                value, hex(value)
            )
        elif header[:4] == b"\x7fELF":
            machine = {0x3E: "x86-64", 0xB7: "arm64"}.get(header[18], hex(header[18]))
        else:
            machine = "native"
    except OSError:  # pragma: no cover - diagnostics only
        pass

    return f"{path} ({machine})"


async def compiler_version() -> str:
    """Return the compiler's version string.

    Returns:
        What ``mpy-cross --version`` reports.
    """
    return await anyio.to_thread.run_sync(mpy_cross_v6.mpy_cross_version)


def _compile_sync(name: str, source: str) -> bytes:
    completed, mpy = mpy_cross_v6.mpy_cross_compile(name, source)
    if completed.returncode != 0 or mpy is None:
        output = completed.stderr.decode("utf-8", "replace") if completed.stderr else ""
        raise CompileError(f"could not compile {name}", output)

    return mpy


SOURCE_NAME = "<string>"
"""The file name to record for source that did not come from a file.

The compiled module carries a name, which is what the hub prints in a
traceback, and there is no file to name. CPython prints ``<string>`` for
``python -c``, so a traceback from ``pb run -c`` reads the way a user who knows
that already expects it to.
"""


def pack_program(modules: Sequence[tuple[str, bytes]]) -> bytes:
    """Pack compiled modules into the container the hub stores.

    The hub reads stored program data as a run of modules, each one a 32-bit
    little-endian size, a zero-terminated module name, and that module's
    ``.mpy`` bytes -- with no padding or alignment between them. There is no
    bare-``.mpy`` alternative: the firmware parses this header before looking
    at the MicroPython magic, so a bare ``.mpy`` is read as a garbage header
    and refused.

    Args:
        modules: Name and compiled bytes for each module. **The one the hub
            starts goes first**: current firmware runs whichever module comes
            first, whatever it is called, and takes the program's displayed
            name from it. Before Pybricks profile v1.5.0 it was found by the
            name ``__main__`` instead, so a hub that old needs the first one
            called that -- which is what
            [CompiledProgram.pack][pb_client.compile.CompiledProgram.pack]
            decides.

    Returns:
        The bytes to store on the hub.
    """
    parts: list[bytes] = []

    for name, mpy in modules:
        parts.append(len(mpy).to_bytes(4, "little"))
        parts.append(name.encode() + b"\x00")
        parts.append(mpy)

    return b"".join(parts)


async def _compile(source: str, *, name: str) -> bytes:
    """Compile one module.

    Args:
        source: The Python code.
        name: What to call it in compiler diagnostics and in tracebacks from
            the hub. A file name, where there is a file.

    Returns:
        Its ``.mpy`` bytes, unpacked.

    Raises:
        CompileError: If the compiler rejects it.
    """
    logger.info("compiling %s (%d bytes of source)", name, len(source))

    try:
        mpy = await anyio.to_thread.run_sync(_compile_sync, name, source)
    except subprocess.SubprocessError as exc:  # pragma: no cover - environment
        raise CompileError(f"could not run the compiler: {exc}") from exc

    logger.info("compiled %s to %d bytes", name, len(mpy))
    return mpy


async def compile_source(source: str, *, name: str = SOURCE_NAME) -> bytes:
    """Compile Python source into a program the hub can run.

    For source that did not come from a file, so there is nothing to discover:
    a program typed at a shell cannot import a module of its own, since there
    is no directory it came from. The one module is called ``__main__``, which
    every firmware runs -- there is no file name to call it instead, and
    ``__main__`` is also what the hub would then display.

    Args:
        source: The Python code.
        name: What to call it in compiler diagnostics and in tracebacks from
            the hub. Defaults to
                [SOURCE_NAME][pb_client.compile.SOURCE_NAME].

    Returns:
        The compiled program, ready to download.

    Raises:
        CompileError: If the compiler rejects it.
    """
    return pack_program([(MAIN_MODULE, await _compile(source, name=name))])


async def _read(path: Path) -> str:
    """Read a source file, blaming it by name if that fails.

    Args:
        path: The file.

    Returns:
        Its text.

    Raises:
        CompileError: If it cannot be read.
    """

    try:
        # Off the event loop: reading a file is blocking, and a script can
        # live on a slow or remote filesystem.
        return await anyio.Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        # strerror rather than str(exc), which repeats the file name and the
        # errno: "could not read demo.py: [Errno 2] No such file or
        # directory: 'demo.py'" says one thing three times.
        reason = exc.strerror or str(exc)
        raise CompileError(f"could not read {path}: {reason.lower()}") from exc


async def _already_compiled(module: Module) -> CompiledModule:
    """Take a module that arrives compiled, checking it will be accepted.

    Args:
        module: The module, whose file is a ``.mpy``.

    Returns:
        It, ready to pack.

    Raises:
        CompileError: If it cannot be read, or is not a ``.mpy`` this hub
            firmware would accept.
    """

    try:
        mpy = await anyio.Path(module.path).read_bytes()
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise CompileError(f"could not read {module.path}: {reason.lower()}") from exc

    # The same two checks the firmware makes before it will run anything. It
    # refuses the whole program without saying which module was wrong, so
    # they are much more useful here.
    if not mpy.startswith(b"M"):
        raise CompileError(f"cannot use {module.path}: it is not a .mpy file")

    if mpy[1] != MPY_ABI_VERSION:
        raise CompileError(
            f"cannot use {module.path}: it is .mpy version {mpy[1]}, but the "
            f"hub needs version {MPY_ABI_VERSION}"
        )

    logger.info("%s arrived compiled, %d bytes", module.name, len(mpy))
    return CompiledModule(module.name, mpy)


async def compile_program(
    entry: Path, *, roots: Sequence[Path] | None = None
) -> CompiledProgram:
    """Compile a script and every module of its own that it imports.

    Args:
        entry: The script the program starts at.
        roots: The directories module names are looked for in, in order.
            Defaults to the script's own directory, which is right for a
            program whose modules sit beside it;
            ``pb_core.project.module_roots`` is what works the list out for a
            project that keeps its source somewhere else.

    Returns:
        The compiled modules, entry first, ready to be packed once the hub's
        profile is known.

    Raises:
        CompileError: If a file cannot be read, the compiler rejects one, a
            module that arrives compiled is not one the hub could run, or a
            module imports relative to its own package -- which the hub cannot
            do, so it is refused here where the line can be named.
    """

    try:
        found = await discover(entry, roots if roots is not None else (entry.parent,))
    except OSError as exc:
        # Which file could not be read matters: with several modules, "no such
        # file" on its own would not say which import was wrong.
        reason = exc.strerror or str(exc)
        raise CompileError(
            f"could not read {exc.filename or entry}: {reason.lower()}"
        ) from exc
    except RelativeImportError as exc:
        # A user error rather than a bug, so it gets the same friendly
        # treatment as a syntax error rather than a traceback.
        raise CompileError(str(exc)) from exc

    compiled: list[CompiledModule] = []

    for module in found:
        if module.precompiled:
            compiled.append(await _already_compiled(module))
            continue

        source = await _read(module.path)
        # The file's name rather than the module's: this is what the hub
        # prints in a traceback, and a traceback should name a file somebody
        # can open.
        mpy = await _compile(source, name=module.path.name)
        compiled.append(CompiledModule(module.name, mpy))

    return CompiledProgram(tuple(compiled))


async def compile_script(path: Path, *, roots: Sequence[Path] | None = None) -> bytes:
    """Compile a script into a program, for a hub whose profile is not known.

    Packs immediately with the modern naming, so it is for callers with no hub
    in hand -- ``pb check``, which only wants to know that a program builds
    and how large it comes out. Anything about to send a program to a hub
    should use [compile_program][pb_client.compile.compile_program] and pack
    once the hub has said which profile it speaks.

    Args:
        path: The script to compile.
        roots: The directories module names are looked for in.

    Returns:
        The compiled program.

    Raises:
        CompileError: If the file cannot be read or the compiler rejects it.
    """
    program = await compile_program(path, roots=roots)
    return program.pack()
