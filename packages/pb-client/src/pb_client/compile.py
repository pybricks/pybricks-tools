# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Compiling Python for a hub.

Compilation happens on the client rather than in the daemon, for two reasons.
The script is on the caller's filesystem, so the daemon would otherwise need to
read files belonging to whoever asked -- which stops being reasonable the moment
the daemon is on another machine. And it keeps the compiler, which is a native
binary, out of the daemon entirely.

The compiler is a subprocess, so it runs in a worker thread rather than on the
event loop.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import anyio
import anyio.to_thread
import mpy_cross_v6

from pb_client.errors import PbClientError

logger = logging.getLogger(__name__)

__all__ = ["CompileError", "compile_script", "compiler_version", "describe_compiler"]


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


async def compile_script(path: Path) -> bytes:
    """Compile a Python file into a program the hub can run.

    A script with no local imports compiles to a bare ``.mpy``, which every
    supported hub accepts. Programs spanning several modules need the
    multi-module container format, which is not implemented yet.

    Args:
        path: The script to compile.

    Returns:
        The compiled program.

    Raises:
        CompileError: If the file cannot be read or the compiler rejects it.
    """
    try:
        # Off the event loop: reading a file is blocking, and a script can
        # live on a slow or remote filesystem.
        source = await anyio.Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        # strerror rather than str(exc), which repeats the file name and the
        # errno: "could not read demo.py: [Errno 2] No such file or
        # directory: 'demo.py'" says one thing three times.
        reason = exc.strerror or str(exc)
        raise CompileError(f"could not read {path}: {reason.lower()}") from exc

    logger.info("compiling %s (%d bytes of source)", path, len(source))
    try:
        mpy = await anyio.to_thread.run_sync(_compile_sync, path.name, source)
    except subprocess.SubprocessError as exc:  # pragma: no cover - environment
        raise CompileError(f"could not run the compiler: {exc}") from exc

    logger.info("compiled %s to %d bytes", path.name, len(mpy))
    return mpy
