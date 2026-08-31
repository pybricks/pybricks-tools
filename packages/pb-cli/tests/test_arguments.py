# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What the commands accept, and how they refuse what they cannot."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Any

import cyclopts
import pytest

from pb_cli import app as pb_app
from pb_cli import cli, errors
from pb_cli import console as console_module
from pb_cli.commands import repl as repl_command


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


def _subcommands(app: cyclopts.App) -> list[str]:
    """Name every real subcommand, leaving out --help, --version and friends."""
    return [name for name in app if not name.startswith("-")]


def _commands(app: cyclopts.App, path: str = "pb") -> Iterator[tuple[str, Any]]:
    """Yield every command in the app and its subapps, with the function behind it."""
    for name in _subcommands(app):
        sub = app[name]
        if _subcommands(sub):
            yield from _commands(sub, f"{path} {name}")
        elif sub.default_command is not None:
            yield f"{path} {name}", sub.default_command


@pytest.mark.anyio
async def test_a_session_without_a_terminal_is_refused_in_words(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An error's headline is printed literally, since it can carry a hub name
    # or a path, so markup written into one arrives on screen as itself. This
    # message used to say "[bold]pb repl[/] needs a terminal".
    monkeypatch.setattr(
        console_module, "why_unavailable", lambda: "standard input is a pipe"
    )
    assert await repl_command.repl_cmd() == errors.EXIT_ERROR
    printed = capsys.readouterr().err
    assert "pb repl needs a terminal" in printed
    assert "[bold]" not in printed


def test_every_top_level_command_is_in_a_named_group() -> None:
    # The help lists commands under headings, and a command with no group
    # lands in the leftover panel with --help and --version, where nobody is
    # looking for it. There is no default that reads well, so each one says
    # where it belongs.
    known = {
        pb_app.HUB_COMMANDS.name,
        pb_app.PROGRAM_COMMANDS.name,
        pb_app.FIRMWARE_COMMANDS.name,
        pb_app.SERVICE_COMMANDS.name,
    }
    for name in _subcommands(cli.app):
        # cyclopts allows one group or several, named or as objects.
        declared = cli.app[name].group
        groups = {
            group if isinstance(group, str) else group.name
            for group in (declared if isinstance(declared, tuple) else (declared,))
        }
        assert groups, f"pb {name} is in no group"
        assert groups <= known, f"pb {name} is in {groups}"


def test_the_hub_is_always_an_option_and_never_positional() -> None:
    # Which hub means the same thing in every command, so it has to look the
    # same in every command. A positional hub also has to be counted past --
    # "was it the first argument or the second?" -- and collides with the
    # arguments that really are per-command.
    for name, func in _commands(cli.app):
        parameter = inspect.signature(func).parameters.get("hub")
        if parameter is None:
            continue
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{name} takes the hub positionally"
        )


def test_every_command_that_talks_to_a_hub_can_be_told_which_one() -> None:
    # The service can hold several hubs at once, so any command that acts on one
    # needs a way to say which. Missing it on a single command is the sort of
    # gap nobody notices until they have two hubs on the desk.
    acting_on_a_hub = {
        "pb connect",
        "pb disconnect",
        "pb status",
        "pb start",
        "pb stop",
        "pb run",
        "pb download",
    }
    found = {
        name
        for name, func in _commands(cli.app)
        if "hub" in inspect.signature(func).parameters
    }
    assert acting_on_a_hub <= found


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["run"], id="neither"),
        pytest.param(["run", "demo.py", "-c", "print(1)"], id="both"),
    ],
)
def test_run_needs_exactly_one_of_a_script_and_c(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # A traceback here would be absurd -- the user has simply mistyped -- and
    # so would running something they did not ask for.
    # cyclopts turns an int result into the process exit code itself.
    with pytest.raises(SystemExit) as info:
        cli.app(argv)
    assert info.value.code == errors.EXIT_ERROR
    captured = capsys.readouterr()
    assert "Give a script to run" in captured.err
    # Nothing about a failure belongs on stdout.
    assert captured.out == ""
