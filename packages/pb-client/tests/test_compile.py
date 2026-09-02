# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Compiling a program of one file or several."""

from __future__ import annotations

from pathlib import Path

import pytest

from pb_client import compile as compiling
from pb_client.models import HubInfo

pytestmark = pytest.mark.anyio


def script(directory: Path, name: str, source: str) -> Path:
    """Write a module and return its path."""
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def a_mpy(version: int = compiling.MPY_ABI_VERSION) -> bytes:
    """Build something with a plausible .mpy header."""
    return bytes([ord("M"), version, 0x00, 0x1F]) + b"\x00" * 8


# ----------------------------------------------------------- already compiled


async def test_a_precompiled_module_passes_through(tmp_path: Path) -> None:
    # The only way to use a module built for a native architecture, which
    # cannot be produced from source here.
    entry = script(tmp_path, "demo.py", "import native\n")
    (tmp_path / "native.mpy").write_bytes(a_mpy())

    compiled = await compiling.compile_program(entry, roots=[tmp_path])

    assert [module.name for module in compiled.modules] == ["demo", "native"]
    assert compiled.modules[1].mpy == a_mpy()


async def test_a_precompiled_module_of_the_wrong_version_is_refused(
    tmp_path: Path,
) -> None:
    # The firmware checks this byte too, and refuses the whole program without
    # saying which module was wrong -- so it is much more useful said here.
    entry = script(tmp_path, "demo.py", "import native\n")
    (tmp_path / "native.mpy").write_bytes(a_mpy(version=5))

    with pytest.raises(compiling.CompileError) as caught:
        await compiling.compile_program(entry, roots=[tmp_path])

    assert "native.mpy" in str(caught.value)
    assert "version 5" in str(caught.value)


async def test_something_that_is_not_a_mpy_at_all_is_refused(tmp_path: Path) -> None:
    entry = script(tmp_path, "demo.py", "import native\n")
    (tmp_path / "native.mpy").write_bytes(b"not a mpy file")

    with pytest.raises(compiling.CompileError, match=r"not a \.mpy"):
        await compiling.compile_program(entry, roots=[tmp_path])


# ------------------------------------------------------------------- failures


async def test_a_missing_module_is_left_to_the_firmware(tmp_path: Path) -> None:
    # pybricks.hubs is on the hub already, so not finding it is the normal
    # case rather than an error.
    entry = script(tmp_path, "demo.py", "from pybricks.hubs import ThisHub\n")
    compiled = await compiling.compile_program(entry, roots=[tmp_path])
    assert len(compiled.modules) == 1


async def test_a_relative_import_is_a_friendly_error(tmp_path: Path) -> None:
    # The hub raises NotImplementedError for these, so refusing here -- as a
    # CompileError, which the command line renders as a sentence rather than a
    # traceback -- is the difference between a puzzle and an answer.
    entry = script(tmp_path, "demo.py", "import robot.motors\n")
    script(tmp_path / "robot", "motors.py", "from . import wheels\n")

    with pytest.raises(compiling.CompileError) as caught:
        await compiling.compile_program(entry, roots=[tmp_path])

    assert "relative import" in str(caught.value)
    assert "motors.py" in str(caught.value)


async def test_an_unreadable_module_names_itself(tmp_path: Path) -> None:
    # With several modules, "no such file" alone would not say which import
    # was wrong.
    entry = script(tmp_path, "demo.py", "import helpers\n")
    missing = tmp_path / "helpers.py"
    missing.write_text("", encoding="utf-8")
    missing.unlink()

    # Nothing to read, so the module is simply not local. The entry script
    # itself is the case that must name a file.
    compiled = await compiling.compile_program(entry, roots=[tmp_path])
    assert len(compiled.modules) == 1

    with pytest.raises(compiling.CompileError) as caught:
        await compiling.compile_program(tmp_path / "gone.py", roots=[tmp_path])

    assert "gone.py" in str(caught.value)


async def test_a_syntax_error_comes_from_the_compiler(tmp_path: Path) -> None:
    # find_imports says nothing about source it cannot parse, deliberately:
    # the compiler names the line, which is a better message.
    entry = script(tmp_path, "demo.py", "def (:\n")

    with pytest.raises(compiling.CompileError) as caught:
        await compiling.compile_program(entry, roots=[tmp_path])

    assert "demo.py" in str(caught.value)


# --------------------------------------------------------------- entry naming


async def test_packing_twice_gives_two_names(tmp_path: Path) -> None:
    # One compile, two hubs: the whole reason compiling and packing are
    # separate steps.
    entry = script(tmp_path, "demo.py", "print('hi')\n")
    compiled = await compiling.compile_program(entry, roots=[tmp_path])

    modern, _, _ = compiled.pack()[4:].partition(b"\x00")
    legacy, _, _ = compiled.pack(legacy_main=True)[4:].partition(b"\x00")
    assert modern == b"demo"
    assert legacy == b"__main__"


def hub(protocol: str) -> HubInfo:
    """A hub reporting a given Pybricks profile version."""
    return HubInfo(
        id="ble:x",
        name="Bumblebee",
        state="connected",
        hub_type="City hub",
        firmware="4.1.0",
        protocol=protocol,
        address="",
        transport="ble",
        num_slots=1,
        max_program_size=100_000,
        subscribers=0,
        program_running=False,
        status_names=(),
    )


@pytest.mark.parametrize(
    ("protocol", "legacy"),
    [
        ("1.2.0", True),
        ("1.3.0", True),
        ("1.4.0", True),
        ("1.5.0", False),
        ("1.5.1", False),
        ("1.6.0", False),
        ("2.0.0", False),
        # Unparseable: __main__ runs on every firmware, where a file-named
        # entry module runs only on the newer ones, so the safe answer is the
        # legacy one.
        ("", True),
        ("who knows", True),
    ],
)
def test_which_hubs_need_the_legacy_main_module(protocol: str, legacy: bool) -> None:
    assert hub(protocol).legacy_main_module is legacy
