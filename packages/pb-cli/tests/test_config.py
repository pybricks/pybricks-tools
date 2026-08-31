# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Where an option comes from when it is not on the command line.

The layers are tested for what they do rather than for how they are spelled: a
small app is built with the same sources the real one uses, and asked what
reached its command.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import cyclopts
import pytest

from pb_cli import config


def _app() -> tuple[cyclopts.App, list[str | None]]:
    """Build an app with the real layers, and somewhere to record what arrived."""
    seen: list[str | None] = []
    app = cyclopts.App(name="probe", config=config.config_sources())

    @app.command(name="takes-a-hub")
    def _takes_a_hub(*, hub: str | None = None) -> None:
        seen.append(hub)

    @app.command(name="takes-no-hub")
    def _takes_no_hub(*, thing: int = 1) -> None:
        seen.append(str(thing))

    return app, seen


def _run(app: cyclopts.App, argv: list[str]) -> None:
    """Run one command, absorbing the exit cyclopts makes on the way out."""
    with contextlib.suppress(SystemExit):
        app(argv, exit_on_error=False)


def _project(directory: Path, body: str) -> None:
    (directory / config.PROJECT_FILE).write_text(body, encoding="utf-8")


def test_a_project_names_its_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, '[tool.pb]\nhub = "Robot A"\n')
    monkeypatch.chdir(tmp_path)
    app, seen = _app()
    _run(app, ["takes-a-hub"])
    assert seen == ["Robot A"]


def test_the_project_is_found_from_anywhere_inside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The point of walking up: which hub a command acts on should not depend on
    # which folder of the project the shell happens to be in.
    _project(tmp_path, '[tool.pb]\nhub = "Robot A"\n')
    deep = tmp_path / "programs" / "line-follower"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    app, seen = _app()
    _run(app, ["takes-a-hub"])
    assert seen == ["Robot A"]


def test_the_environment_beats_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, '[tool.pb]\nhub = "Robot A"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PB_HUB", "Robot B")
    app, seen = _app()
    _run(app, ["takes-a-hub"])
    assert seen == ["Robot B"]


def test_what_was_typed_beats_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, '[tool.pb]\nhub = "Robot A"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PB_HUB", "Robot B")
    app, seen = _app()
    _run(app, ["takes-a-hub", "--hub", "Robot C"])
    assert seen == ["Robot C"]


def test_a_command_with_no_hub_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Not a detail: a hub in the file is matched against the parameters of
    # whichever command is running, so being strict about unknown keys would
    # make pb check and pb list fail merely because the file exists.
    _project(tmp_path, '[tool.pb]\nhub = "Robot A"\n')
    monkeypatch.chdir(tmp_path)
    app, seen = _app()
    _run(app, ["takes-no-hub"])
    assert seen == ["1"]


# ------------------------------------------------------ what the file may say


def test_a_file_with_nothing_to_do_with_us_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, '[project]\nname = "robot"\n')
    monkeypatch.chdir(tmp_path)
    config.check_project_file()


def test_no_file_at_all_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert config.find_project_file() is None
    config.check_project_file()


def test_a_key_we_do_not_understand_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole reason for reading the file ourselves. cyclopts has to be told
    # to ignore what it does not recognise, so without this a misspelled key
    # would simply never be applied, and the hub would silently be the wrong
    # one.
    _project(tmp_path, '[tool.pb]\nhubb = "Robot A"\n')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(config.ProjectConfigError) as caught:
        config.check_project_file()
    assert "hubb" in str(caught.value)
    assert caught.value.path.name == config.PROJECT_FILE


def test_every_unknown_key_is_named_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, '[tool.pb]\nhub = "A"\nslot = 1\nwait = true\n')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(config.ProjectConfigError) as caught:
        config.check_project_file()
    said = str(caught.value)
    assert "slot" in said
    assert "wait" in said
    # The one that is understood is not complained about.
    assert "hub" not in said.replace("hubb", "")


def test_a_file_that_is_not_toml_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, "[tool.pb\nhub =\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(config.ProjectConfigError) as caught:
        config.check_project_file()
    assert "TOML" in str(caught.value)


def test_a_pb_table_that_is_not_a_table_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, '[tool]\npb = "Robot A"\n')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(config.ProjectConfigError):
        config.check_project_file()
