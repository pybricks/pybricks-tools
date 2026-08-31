# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""``pb -C``: running a command as if from somewhere else.

Tested through :func:`pb_cli.cli.launcher` rather than by spawning ``pb``,
because what it does -- change the working directory and settle which project
file applies -- is exactly what a subprocess would hide.

Every test changes directory through ``monkeypatch.chdir`` first, so that
pytest puts the real working directory back afterwards whatever the launcher
does to it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import cyclopts
import pytest

from pb_cli import cli, config

# Something that needs no hub, no service and no network, so these tests are
# about the launcher rather than about the command it dispatches to.
QUIET_COMMAND = ("check", "-c", "print(1)")


@pytest.fixture(autouse=True)
def _leave_the_app_as_it_was() -> Iterator[None]:
    """Put back the config layers the launcher replaces.

    ``app.config`` is a global, and a test that leaves it pointing at its own
    temporary directory would quietly change what a later test reads.

    Yields:
        Nothing.
    """
    original = cli.app.config
    try:
        yield
    finally:
        cli.app.config = original


def run(*tokens: str, directory: Path | None = None) -> int:
    """Run the launcher and return the exit code, however it leaves.

    cyclopts turns a command's return value into ``sys.exit``, so a command
    that succeeded arrives here as ``SystemExit(0)``.

    Args:
        tokens: The command and its arguments.
        directory: What to pass as ``-C``.

    Returns:
        The exit code.
    """
    try:
        return cli.launcher(*tokens, directory=directory)
    except SystemExit as leaving:
        return int(leaving.code or 0)


def project(directory: Path, body: str) -> Path:
    """Write a project file, making its directory."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / config.PROJECT_FILE
    path.write_text(body, encoding="utf-8")
    return path


def toml_source() -> cyclopts.config.Toml:
    """The project-file layer the launcher last handed to cyclopts."""
    sources = cli.app.config
    assert isinstance(sources, tuple)
    found = [item for item in sources if isinstance(item, cyclopts.config.Toml)]
    assert len(found) == 1
    return found[0]


def test_it_runs_the_command_from_the_named_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "robot-a"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)

    assert run(*QUIET_COMMAND, directory=elsewhere) == 0

    assert Path.cwd().resolve() == elsewhere.resolve()


def test_without_it_the_directory_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert run(*QUIET_COMMAND) == 0
    assert Path.cwd().resolve() == tmp_path.resolve()


def test_a_directory_that_is_not_there_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A typo, not a crash, so it gets a sentence rather than a traceback.
    monkeypatch.chdir(tmp_path)
    assert run(*QUIET_COMMAND, directory=tmp_path / "nope") != 0
    assert Path.cwd().resolve() == tmp_path.resolve()


def test_a_file_is_not_a_directory_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    not_a_directory = tmp_path / "demo.py"
    not_a_directory.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert run(*QUIET_COMMAND, directory=not_a_directory) != 0


def test_it_settles_which_project_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The monorepo case: two projects, and the shell standing in neither.

    Without -C the answer depends on where the shell happens to be, which for
    something as physical as which hub a motor turns on is not good enough.
    """
    a = project(tmp_path / "robot-a", '[tool.pb]\nhub = "Bumblebee"\n')
    project(tmp_path / "robot-b", '[tool.pb]\nhub = "Rover"\n')
    monkeypatch.chdir(tmp_path)

    assert run(*QUIET_COMMAND, directory=tmp_path / "robot-a") == 0

    assert Path(toml_source().path) == a


def test_a_member_with_no_settings_does_not_shadow_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monorepo names its hub once at the top and overrides it per project.

    The member has a pyproject.toml for entirely unrelated reasons -- being a
    package -- and stopping there would turn the feature off silently.
    """
    top = project(tmp_path, '[tool.pb]\nhub = "Bumblebee"\n')
    member = tmp_path / "robot-a"
    project(member, '[project]\nname = "robot-a"\nversion = "1.0"\n')
    monkeypatch.chdir(tmp_path)

    assert run(*QUIET_COMMAND, directory=member) == 0

    assert Path(toml_source().path) == top


def test_a_broken_project_file_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unparseable is worth saying so about, where a file that simply says
    # nothing about this tool is searched past in silence.
    project(tmp_path, "this is not toml {")
    monkeypatch.chdir(tmp_path)
    assert run(*QUIET_COMMAND) != 0


def test_an_unknown_key_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The strictness the project file already had, still applied, and now
    # applied to the file that -C selected.
    project(tmp_path / "robot-a", '[tool.pb]\nhubb = "Bumblebee"\n')
    monkeypatch.chdir(tmp_path)
    assert run(*QUIET_COMMAND, directory=tmp_path / "robot-a") != 0
