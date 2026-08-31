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
from typing import cast

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


def config_sources(
    project_file: Path | None = None,
) -> tuple[cyclopts.config.Env, cyclopts.config.Toml]:
    """Build the layers an option falls back to when it is not given.

    Args:
        project_file: The project file to read, when it has already been
            found. Given by the launcher, which knows where ``-C`` pointed and
            applies the "nearest file with a ``[tool.pb]`` table" rule. Left
            out, cyclopts searches upwards from the working directory itself
            and stops at the first ``pyproject.toml`` it finds, which is the
            older and less careful behaviour.

    Returns:
        The sources, in the order cyclopts consults them: the environment
        first, then the project file. Anything typed on the command line beats
        both.
    """
    return (
        cyclopts.config.Env(ENV_PREFIX, command=False),
        cyclopts.config.Toml(
            project_file if project_file is not None else PROJECT_FILE,
            root_keys=("tool", "pb"),
            # From the working directory upwards, so it is the project that
            # matters rather than where in it the command was run.
            search_parents=project_file is None,
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


def _settings(path: Path) -> dict[str, object] | None:
    """Read one file's ``[tool.pb]`` table.

    Args:
        path: A ``pyproject.toml``.

    Returns:
        The table, or ``None`` if the file does not exist or has no
        ``[tool.pb]``.

    Raises:
        ProjectConfigError: If the file exists but cannot be read or parsed,
            or if ``[tool.pb]`` is not a table. A file that cannot be read is
            worth saying so about, where a file that simply says nothing about
            this tool is not.
    """
    if not path.is_file():
        return None
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProjectConfigError(f"{PROJECT_FILE} could not be read.", path) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ProjectConfigError(f"{PROJECT_FILE} is not valid TOML.", path) from exc

    table = document.get("tool", {})
    settings = table.get("pb") if isinstance(table, dict) else None
    if settings is None:
        return None
    if not isinstance(settings, dict):
        raise ProjectConfigError("[tool.pb] is not a table.", path)
    return cast("dict[str, object]", settings)


def find_project_file(start: Path | None = None) -> Path | None:
    """Find the project file that applies here.

    A file with no ``[tool.pb]`` is searched *past* rather than stopping the
    search. Otherwise a monorepo cannot name its hub once at the top and
    override it per project -- a member's own ``pyproject.toml``, which it has
    for entirely unrelated reasons, would shadow it -- and an unrelated project
    file anywhere in the path would turn the feature off silently, which is the
    worst way for it to be off.

    Args:
        start: Where to look from, defaulting to the working directory.

    Returns:
        The nearest ``pyproject.toml`` at or above ``start`` that has a
        ``[tool.pb]`` table, or ``None``.

    Raises:
        ProjectConfigError: If a candidate cannot be read or parsed.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / PROJECT_FILE
        if _settings(candidate) is not None:
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
    settings = _settings(path)
    if settings is None:  # pragma: no cover - find_project_file just read it
        return

    unknown = sorted(set(settings) - KNOWN_KEYS)
    if unknown:
        named = ", ".join(unknown)
        raise ProjectConfigError(
            f"[tool.pb] does not understand {named}.",
            path,
        )
