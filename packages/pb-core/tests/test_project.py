# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

from pathlib import Path

import pytest

from pb_core import project

pytestmark = pytest.mark.anyio

MINIMAL = '[project]\nname = "robot"\nversion = "1.0"\n'
"""Enough of a project file to count as one."""


def make(directory: Path, *, extra: str = "", src: bool = False) -> Path:
    """Write a project file, optionally with a src directory beside it."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / project.PROJECT_FILE
    path.write_text(MINIMAL + extra, encoding="utf-8")

    if src:
        (directory / "src").mkdir(exist_ok=True)

    return path


# --------------------------------------------------------- what each backend says


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        # uv_build
        ('[tool.uv.build-backend]\nmodule-root = "lib"\n', "lib"),
        # ...and its way of spelling a flat layout.
        ('[tool.uv.build-backend]\nmodule-root = ""\n', ""),
        # setuptools, both spellings.
        ('[tool.setuptools]\npackage-dir = {"" = "lib"}\n', "lib"),
        ('[tool.setuptools.packages.find]\nwhere = ["lib"]\n', "lib"),
        # hatchling: sources as a mapping and as a list.
        ('[tool.hatch.build.targets.wheel.sources]\n"lib" = "robot"\n', "lib"),
        ('[tool.hatch.build.targets.wheel]\nsources = ["lib"]\n', "lib"),
        # hatchling: packages names the package, so the root is its parent.
        ('[tool.hatch.build.targets.wheel]\npackages = ["lib/robot"]\n', "lib"),
        ('[tool.hatch.build.targets.wheel]\nonly-include = ["lib/robot"]\n', "lib"),
        # ...and a package at the top level means a flat layout.
        ('[tool.hatch.build.targets.wheel]\npackages = ["robot"]\n', ""),
        # poetry
        (
            '[tool.poetry]\npackages = [{include = "robot", from = "lib"}]\n',
            "lib",
        ),
        # pdm-backend and maturin
        ('[tool.pdm.build]\npackage-dir = "lib"\n', "lib"),
        ('[tool.maturin]\npython-source = "lib"\n', "lib"),
    ],
)
async def test_a_declared_source_directory_is_used(
    tmp_path: Path, extra: str, expected: str
) -> None:
    # A src/ directory is present to prove the declaration beats the
    # convention rather than merely agreeing with it.
    path = make(tmp_path, extra=extra, src=True)
    assert await project.source_root(path) == (
        tmp_path / expected if expected else tmp_path
    )


async def test_src_is_assumed_when_it_exists(tmp_path: Path) -> None:
    # The documented default for uv_build and what setuptools auto-detects,
    # so nothing has to be declared for the common case.
    path = make(tmp_path, src=True)
    assert await project.source_root(path) == tmp_path / "src"


async def test_a_flat_project_keeps_its_own_directory(tmp_path: Path) -> None:
    path = make(tmp_path)
    assert await project.source_root(path) == tmp_path


async def test_an_unreadable_backend_table_falls_through(tmp_path: Path) -> None:
    # Something in the right place of the wrong type says nothing, rather
    # than raising in the middle of running a program.
    path = make(tmp_path, extra="[tool.uv.build-backend]\nmodule-root = 3\n", src=True)
    assert await project.source_root(path) == tmp_path / "src"


# ------------------------------------------------------------- finding a project


async def test_the_nearest_project_wins(tmp_path: Path) -> None:
    make(tmp_path)
    inner = make(tmp_path / "robot-a")
    assert await project.find_project(tmp_path / "robot-a") == inner


async def test_a_project_is_found_from_a_subdirectory(tmp_path: Path) -> None:
    path = make(tmp_path)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert await project.find_project(deep) == path


async def test_a_file_with_no_project_table_is_searched_past(tmp_path: Path) -> None:
    # A member of a monorepo carrying only [tool.something] must not shadow
    # the project above it, or a workspace cannot be configured at the top.
    outer = make(tmp_path)
    member = tmp_path / "robot-a"
    member.mkdir()
    (member / project.PROJECT_FILE).write_text(
        "[tool.uv.sources]\nfoo = { workspace = true }\n", encoding="utf-8"
    )
    assert await project.find_project(member) == outer


async def test_broken_toml_is_not_this_tools_problem(tmp_path: Path) -> None:
    outer = make(tmp_path)
    member = tmp_path / "robot-a"
    member.mkdir()
    (member / project.PROJECT_FILE).write_text("this is not toml {", encoding="utf-8")
    assert await project.find_project(member) == outer


async def test_no_project_at_all(tmp_path: Path) -> None:
    assert await project.find_project(tmp_path) is None


# ----------------------------------------------------------------- the whole rule


async def test_a_named_directory_with_a_project(tmp_path: Path) -> None:
    make(tmp_path / "robot-a", src=True)
    entry = tmp_path / "robot-a" / "src" / "robot" / "demo.py"
    entry.parent.mkdir(parents=True)
    entry.touch()
    roots = await project.module_roots(entry, directory=tmp_path / "robot-a")
    # The script's own directory first, as Python does, then the project's
    # source root -- which is what gives the entry module its dotted name.
    assert roots == (
        tmp_path / "robot-a" / "src" / "robot",
        tmp_path / "robot-a" / "src",
    )


async def test_a_named_directory_with_no_project_is_the_root(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    entry = scripts / "sub" / "demo.py"
    entry.parent.mkdir(parents=True)
    entry.touch()
    assert await project.module_roots(entry, directory=scripts) == (
        scripts / "sub",
        scripts,
    )


async def test_with_neither_the_script_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What the tool this replaces always did, and what lets a helper module
    # beside a script work with no configuration anywhere.
    entry = tmp_path / "examples" / "demo.py"
    entry.parent.mkdir(parents=True)
    entry.touch()
    monkeypatch.chdir(tmp_path)
    assert await project.module_roots(entry) == (tmp_path / "examples",)


async def test_the_working_directory_finds_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make(tmp_path, src=True)
    entry = tmp_path / "src" / "robot" / "demo.py"
    entry.parent.mkdir(parents=True)
    entry.touch()
    monkeypatch.chdir(tmp_path / "src" / "robot")
    assert await project.module_roots(entry) == (
        tmp_path / "src" / "robot",
        tmp_path / "src",
    )


async def test_a_script_outside_the_source_tree_keeps_its_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug that made roots plural, found by running the command.

    A project with a src layout and a script kept somewhere else: with src as
    the only root, a helper module sitting beside that script is silently left
    out of the program, which then fails on the hub with an ImportError.
    """
    make(tmp_path, src=True)
    entry = tmp_path / "scratch" / "demo.py"
    entry.parent.mkdir()
    entry.touch()
    monkeypatch.chdir(tmp_path)

    assert await project.module_roots(entry) == (
        tmp_path / "scratch",
        tmp_path / "src",
    )
