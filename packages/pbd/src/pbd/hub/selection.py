# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The hub the user chose, remembered between runs of the service.

A choice is stored as a **name**, because a hub has nothing else durable to
store. Its id is built from a Bluetooth address, which rotates, and which on
macOS is a per-machine UUID standing in for one -- so an id remembered today
names nothing tomorrow. A name is user-settable and is not unique, which is a
real limit and is written up in the design notes; it is also the only thing
about a hub that survives being switched off and on, and surviving that is the
whole job of a choice.

Nothing here may fail a command. A preference that cannot be read is a
preference that was never set, and one that cannot be written still applies
until the service stops. The user asked about a hub rather than about a file,
so a broken file is logged and the work carries on.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pb_core import paths

logger = logging.getLogger(__name__)

__all__ = ["Selection", "selection_file"]

_KEY = "hub"
"""The one field in the file, named so the file explains itself when read."""


def selection_file() -> Path:
    """Return where the chosen hub is remembered."""
    return paths.state_dir() / "selection.json"


class Selection:
    """The chosen hub's name, kept on disk so it outlives the service."""

    def __init__(self, path: Path | None = None) -> None:
        """Load the stored choice.

        Args:
            path: Where to keep it. Defaults to the user's state directory.
        """
        self._path = path if path is not None else selection_file()
        self._name = self._read()

    @property
    def path(self) -> Path:
        """Where the choice is stored."""
        return self._path

    @property
    def name(self) -> str | None:
        """The chosen hub's name, or ``None`` if none has been chosen."""
        return self._name

    def choose(self, name: str | None) -> None:
        """Remember a hub, or forget the one remembered.

        Args:
            name: The hub's name, or ``None`` to clear the choice.
        """
        self._name = name
        self._write()

    def _read(self) -> str | None:
        """Return the stored name, or ``None`` if there is not one to have."""
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("ignoring unreadable %s", self._path, exc_info=True)
            return None
        name = stored.get(_KEY) if isinstance(stored, dict) else None
        return name if isinstance(name, str) and name else None

    def _write(self) -> None:
        """Store the choice, or log why it could not be stored."""
        temporary = self._path.with_name(f"{self._path.name}.new")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps({_KEY: self._name}, indent=2) + "\n", encoding="utf-8"
            )
            # Replace rather than write in place, so a reader either sees the
            # old choice or the new one and never half a file.
            temporary.replace(self._path)
        except OSError:
            logger.warning(
                "could not store the chosen hub in %s", self._path, exc_info=True
            )
