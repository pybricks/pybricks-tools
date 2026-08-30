# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Platform locations for the daemon's socket, lock, cookie, log and cache.

Every path used by more than one process lives here, so that the client and the
daemon cannot disagree about where to look. Callers never type these paths; the
whole point is that connecting to the daemon is transparent.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

import platformdirs

__all__ = [
    "APP_NAME",
    "RUNTIME_DIR_ENV_VAR",
    "STATE_DIR_ENV_VAR",
    "cache_dir",
    "cookie_file",
    "ensure_private_dir",
    "lock_file",
    "log_file",
    "runtime_dir",
    "socket_path",
    "state_dir",
    "windows_pipe_name",
]

APP_NAME = "pybricks"
"""Application name used for all platform directories."""

RUNTIME_DIR_ENV_VAR = "PB_RUNTIME_DIR"
"""Overrides the runtime directory.

Moves the socket, lock and cookie together, so a test or a sandboxed
environment can relocate all of the daemon's per-user state at once without
touching the real one.
"""

STATE_DIR_ENV_VAR = "PB_STATE_DIR"
"""Overrides the directory holding what the service remembers between runs.

The counterpart of :data:`RUNTIME_DIR_ENV_VAR`, and there for the same reason:
a test or a sandbox can put the user's preferences somewhere of its own rather
than editing the real ones.
"""

_DAEMON = "pbd"


def _uid() -> int:
    """Return the current user id, or 0 on platforms without ``os.getuid``."""
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else 0


def runtime_dir() -> Path:
    """Return the directory holding the daemon's socket, lock and cookie.

    On Linux this honours ``XDG_RUNTIME_DIR``, which is the correct location for
    per-user runtime state and is cleaned up at logout.

    On macOS this deliberately does *not* use :func:`platformdirs.user_runtime_dir`,
    which returns a long path under ``/var/folders``. ``sockaddr_un.sun_path`` is
    only 104 bytes on Darwin, so a deterministic short path under the temporary
    directory is used instead.

    On Windows the daemon is reached through a named pipe rather than a socket,
    so this is only used for the lock and cookie files.
    """
    override = os.environ.get(RUNTIME_DIR_ENV_VAR)
    if override:
        return Path(override)

    if sys.platform == "win32":
        return Path(platformdirs.user_runtime_dir(APP_NAME, appauthor=False))

    if sys.platform == "linux":
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg:
            return Path(xdg) / APP_NAME

    # macOS, and Linux without XDG_RUNTIME_DIR: keep it short for sun_path.
    return Path(tempfile.gettempdir()) / f"{APP_NAME}-{_uid()}"


def socket_path() -> Path:
    """Return the AF_UNIX socket path. Not meaningful on Windows."""
    return runtime_dir() / f"{_DAEMON}.sock"


def windows_pipe_name() -> str:
    """Return the Windows named pipe address for the daemon.

    The name includes the user name because pipe names are machine-global, so
    two users on one machine must not collide.
    """
    user = os.environ.get("USERNAME") or "user"
    safe = "".join(c for c in user if c.isalnum() or c in "-_") or "user"
    return rf"\.\pipe\{APP_NAME}-{_DAEMON}-{safe}"


def lock_file() -> Path:
    """Return the single-instance lock file path."""
    return runtime_dir() / f"{_DAEMON}.lock"


def cookie_file() -> Path:
    """Return the path of the shared-secret file used to authenticate clients.

    Required on every transport, not only TCP: the default Windows named-pipe
    DACL grants Everyone read access, so without a shared secret another local
    user could read a hub's output.
    """
    return runtime_dir() / f"{_DAEMON}.cookie"


def state_dir() -> Path:
    """Return the directory for what the service remembers between runs.

    On Linux this is ``$XDG_STATE_HOME``, which is where the spec puts state
    that should persist but is not worth backing up -- which is exactly what a
    remembered hub is.

    Returns:
        The directory, which may not exist yet.
    """
    override = os.environ.get(STATE_DIR_ENV_VAR)
    if override:
        return Path(override)
    return Path(platformdirs.user_state_dir(APP_NAME, appauthor=False))


def cache_dir() -> Path:
    """Return the directory for cached downloads, such as firmware images."""
    return Path(platformdirs.user_cache_dir(APP_NAME, appauthor=False))


def log_file() -> Path:
    """Return the daemon's log file path."""
    return Path(platformdirs.user_log_dir(APP_NAME, appauthor=False)) / f"{_DAEMON}.log"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` if needed and verify it is private to this user.

    The directory is created with mode ``0o700``. On POSIX its ownership and
    mode are then checked, because a shared location such as ``/tmp`` allows
    another user to pre-create the directory and read whatever is placed in it.

    Args:
        path: The directory to create and check.

    Returns:
        The same path, for convenience.

    Raises:
        PermissionError: If the directory exists but is owned by another user or
            is accessible to group or other.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)

    if sys.platform == "win32":
        # Windows has no meaningful st_uid/st_mode here; access control comes
        # from the inherited ACL of the user's own profile directory.
        return path

    info = path.stat()
    if info.st_uid != _uid():
        raise PermissionError(f"{path} is owned by uid {info.st_uid}, not {_uid()}")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"{path} is accessible to other users: {info.st_mode:o}")
    return path
