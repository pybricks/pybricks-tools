# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The ``pb`` command line.

This module is the entry point and little else. The commands live in
:mod:`pb_cli.commands`, one module per group of the help, and importing them
here is what registers them with the app -- the same arrangement the service
uses for its RPC methods.

Three rules shape the whole package. Data goes to stdout and everything else --
progress, spinners, logs, prompts -- goes to stderr, so piping a command into
another program stays clean; :mod:`pb_cli.ui` is where that is enforced. Commands
are thin: they parse arguments, call ``pb_client``, and render, with no
protocol logic anywhere in this package. And no command catches its own
failures, because a task group would have wrapped them in an ``ExceptionGroup``
anyway -- :mod:`pb_cli.errors` does that in one place, for all of them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import cyclopts

# Imported for the side effect: importing a command module registers it with
# the app, so nothing here reads the names. Said on each line, because a
# comment is not something a linter can read.
import pb_cli.commands.hub  # pyright: ignore[reportUnusedImport]
import pb_cli.commands.program  # pyright: ignore[reportUnusedImport]
import pb_cli.commands.repl  # pyright: ignore[reportUnusedImport]
import pb_cli.commands.service  # noqa: F401 # pyright: ignore[reportUnusedImport]
from pb_cli import ui
from pb_cli.app import app
from pb_cli.config import (
    KNOWN_KEYS,
    ProjectConfigError,
    check_project_file,
    config_sources,
    find_project_file,
)
from pb_cli.errors import EXIT_OK

__all__ = ["app", "launcher", "main"]


@app.meta.default
def launcher(
    *tokens: Annotated[str, cyclopts.Parameter(show=False, allow_leading_hyphen=True)],
    directory: Annotated[
        Path | None,
        cyclopts.Parameter(
            # The long name first, because it is what names the environment
            # variable: with -C first that came out as PB_C, which says
            # nothing. Both spellings work either way, and -C is the one
            # anybody will type.
            name=["--directory", "-C"],
            show_default=False,
            # The same panel --help and --version are in, rather than a
            # "Parameters" panel of its own above the commands. Somebody
            # reading the help is looking for the commands first.
            group=cyclopts.Group("Options", sort_key=3),
        ),
    ] = None,
) -> int:
    """Run a command, optionally as if from somewhere else.

    Args:
        tokens: The command to run, and its arguments.
        directory: Work as if run from here, the way ``git -C`` does. Point it
            at the directory holding a project's ``pyproject.toml``, or at the
            root of the source when there is no project file. It settles which
            project's settings apply, what a relative path means, and where a
            program's imports resolve from -- which is what makes a monorepo of
            several robot projects work from any shell.

    Returns:
        The process exit code.
    """

    if directory is not None:
        try:
            os.chdir(directory)
        except OSError as exc:
            # A directory that is not there is a typo, not a crash.
            reason = exc.strerror or str(exc)
            return ui.usage(
                f"Cannot work from {directory}: {reason.lower()}.",
                "Give [bold]-C[/] a directory that exists.",
            )

    try:
        # After the change of directory and before the command runs: a project
        # file that says something unrecognised should be reported rather than
        # quietly not applied, and which file that is depends on -C.
        check_project_file()
        # Hand cyclopts the file this found, rather than letting it search on
        # its own and stop at the first pyproject.toml. See find_project_file.
        app.config = config_sources(find_project_file())
    except ProjectConfigError as exc:
        return ui.usage(
            str(exc),
            # The square bracket is escaped: rich reads "[tool.pb]" as a style
            # tag and swallows it, which is how this line first came out as
            # "Check ...toml.  understands hub."
            f"Check [bold]{exc.path}[/]. [bold]\\[tool.pb][/] understands "
            f"{', '.join(sorted(KNOWN_KEYS))}.",
        )

    result = app(tokens)
    return result if isinstance(result, int) else EXIT_OK


def main() -> int:
    """Run the CLI.

    Returns:
        The process exit code.
    """
    result = app.meta()
    return result if isinstance(result, int) else EXIT_OK
