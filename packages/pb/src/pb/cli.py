# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The ``pb`` command line.

This module is the entry point and little else. The commands live in
:mod:`pb.commands`, one module per group of the help, and importing them
here is what registers them with the app -- the same arrangement the daemon
uses for its RPC methods.

Three rules shape the whole package. Data goes to stdout and everything else --
progress, spinners, logs, prompts -- goes to stderr, so piping a command into
another program stays clean; :mod:`pb.ui` is where that is enforced. Commands
are thin: they parse arguments, call ``pb_client``, and render, with no
protocol logic anywhere in this package. And no command catches its own
failures, because a task group would have wrapped them in an ``ExceptionGroup``
anyway -- :mod:`pb.errors` does that in one place, for all of them.
"""

from __future__ import annotations

# Imported to register the commands.
import pb.commands.hub
import pb.commands.program
import pb.commands.repl
import pb.commands.service  # noqa: F401
from pb import ui
from pb.app import app
from pb.config import KNOWN_KEYS, ProjectConfigError, check_project_file
from pb.errors import EXIT_OK

__all__ = ["app", "main"]


def main() -> int:
    """Run the CLI.

    Returns:
        The process exit code.
    """
    try:
        # Before the command runs, because a project file that says
        # something unrecognised should be reported rather than
        # quietly not applied.
        check_project_file()
    except ProjectConfigError as exc:
        return ui.usage(
            str(exc),
            # The square bracket is escaped: rich reads "[tool.pb]" as a style
            # tag and swallows it, which is how this line first came out as
            # "Check ...toml.  understands hub."
            f"Check [bold]{exc.path}[/]. [bold]\\[tool.pb][/] understands "
            f"{', '.join(sorted(KNOWN_KEYS))}.",
        )
    result = app()
    return result if isinstance(result, int) else EXIT_OK
