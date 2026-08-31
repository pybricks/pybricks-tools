# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Where an option comes from when it is not on the command line.

Three layers, highest first: what was typed, then the environment, then the
project's ``pyproject.toml``. Below them the command decides for itself -- for
a hub that means the one chosen with ``pb use``, and then the only one
connected.

The project layer is what makes a robot project work for whoever clones it:
``[tool.pb] hub`` names the hub once, and every shell opened anywhere inside
the project agrees, without anybody having to remember a flag.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import cyclopts

__all__ = [
    "ENV_PREFIX",
    "KNOWN_KEYS",
    "PROJECT_FILE",
    "ProjectConfigError",
    "check_project_file",
    "config_sources",
    "find_project_file",
]

PROJECT_FILE = "pyproject.toml"
"""Where a project says which hub it belongs to.

Under ``[tool.pb]``, found by walking up from the working directory.
"""

ENV_PREFIX = "PB_"
"""How an option is spelled in the environment: ``--hub`` is ``PB_HUB``.

Every option, not only that one. One prefix is easier to remember than a list
of blessed variables, and an environment variable is ephemeral -- it belongs to
one shell and one moment -- so being generous costs little.
"""

KNOWN_KEYS = frozenset({"hub"})
"""What ``[tool.pb]`` is allowed to contain.

Deliberately a short list rather than "any option this tool happens to have".
The file is committed and read by everyone who clones the project, which makes
it documented surface rather than a convenience, and surface should say what it
covers. Adding a key here is how it grows.
"""


class ProjectConfigError(Exception):
    """The project file says something this tool does not understand."""

    def __init__(self, message: str, path: Path) -> None:
        """Build the error.

        Args:
            message: What is wrong, as a sentence.
            path: The file it is wrong in.
        """
        super().__init__(message)
        self.path = path


def config_sources() -> tuple[cyclopts.config.Env, cyclopts.config.Toml]:
    """Build the layers an option falls back to when it is not given.

    Returns:
        The sources, in the order cyclopts consults them: the environment
        first, then the project file. Anything typed on the command line beats
        both.
    """
    return (
        cyclopts.config.Env(ENV_PREFIX, command=False),
        cyclopts.config.Toml(
            PROJECT_FILE,
            root_keys=("tool", "pb"),
            # From the working directory upwards, so it is the project that
            # matters rather than where in it the command was run.
            search_parents=True,
            # One table for every command rather than a table per command:
            # [tool.pb] hub, not [tool.pb.run] hub.
            use_commands_as_keys=False,
            # Necessary rather than lenient. A key is matched against the
            # parameters of the command being run, so "hub" would otherwise be
            # an unknown option to every command that does not take one -- pb
            # check and pb list among them, which would then fail merely
            # because the file exists. What this gives up, a misspelled key
            # passing unnoticed, is taken back by check_project_file.
            allow_unknown=True,
        ),
    )


def find_project_file(start: Path | None = None) -> Path | None:
    """Find the project file that applies here.

    Args:
        start: Where to look from, defaulting to the working directory.

    Returns:
        The nearest ``pyproject.toml`` at or above ``start``, or ``None``.
        Nearest wins, the same file cyclopts will read.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / PROJECT_FILE
        if candidate.is_file():
            return candidate
    return None


def check_project_file(start: Path | None = None) -> None:
    """Refuse a project file that says something unrecognised.

    Args:
        start: Where to look from, defaulting to the working directory.

    Raises:
        ProjectConfigError: If the file cannot be read, or ``[tool.pb]`` holds
            a key this tool does not understand. Being strict here is the point
            of reading it ourselves: cyclopts has to be told to ignore unknown
            keys, and a hub that is quietly not applied because of a typo is
            exactly the kind of thing somebody spends an afternoon on.
    """
    path = find_project_file(start)
    if path is None:
        return
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProjectConfigError(f"{PROJECT_FILE} could not be read.", path) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ProjectConfigError(f"{PROJECT_FILE} is not valid TOML.", path) from exc

    table = document.get("tool", {})
    settings = table.get("pb") if isinstance(table, dict) else None
    if settings is None:
        return
    if not isinstance(settings, dict):
        raise ProjectConfigError("[tool.pb] is not a table.", path)

    unknown = sorted(set(settings) - KNOWN_KEYS)
    if unknown:
        named = ", ".join(unknown)
        raise ProjectConfigError(
            f"[tool.pb] does not understand {named}.",
            path,
        )
