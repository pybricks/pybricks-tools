# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Starting a daemon on demand.

Two clients running at once may both find no daemon and both start one. That is
deliberately not coordinated: one wins the single-instance lock, the loser exits
quietly, and both clients' readiness loops then connect to the winner. There are
no PID files to go stale and no startup handshake to get wrong.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anyio
from anyio.abc import ByteStream

from pb_client.errors import DaemonStartError, DaemonUnavailable
from pb_core import ipc, paths
from pb_core.ipc import Endpoint

logger = logging.getLogger(__name__)

__all__ = ["DAEMON_COMMAND_ENV_VAR", "open_connection", "spawn_daemon"]

DAEMON_COMMAND_ENV_VAR = "PB_DAEMON_CMD"

READY_TIMEOUT = 15.0
"""How long to keep polling for a daemon we just started.

Deliberately a module constant rather than a parameter. The repository's async
rules say public APIs take no ``timeout`` argument -- callers scope cancellation
with ``anyio.fail_after`` instead. This is not a caller-facing deadline but an
internal safety bound, whose real job is to turn "hung forever" into a message
naming the daemon's log file.
"""
_FIRST_DELAY = 0.02
_MAX_DELAY = 0.25
_BACKOFF = 1.6

_NOT_LISTENING = (ConnectionRefusedError, FileNotFoundError)
"""What "no daemon there" looks like.

On Windows, connecting to a pipe that does not exist raises FileNotFoundError
straight away rather than waiting, so the retry loop is what supplies the
patience.
"""


def _daemon_command() -> list[str]:
    """Return the command that starts a daemon.

    The fallback to ``-m pb_service`` is why the CLI package depends on the daemon
    package even though it never imports it: installing the CLI alone must
    still give a working autostart.
    """
    override = os.environ.get(DAEMON_COMMAND_ENV_VAR)
    if override:
        # posix=False on Windows, or shlex treats the backslashes in a path
        # like C:\Python\python.exe as escapes and silently mangles it. That
        # mode leaves quotes attached, so strip them.
        if sys.platform == "win32":
            return [token.strip('"') for token in shlex.split(override, posix=False)]
        return shlex.split(override)
    found = shutil.which("pb-service")
    if found:
        return [found]
    return [sys.executable, "-m", "pb_service"]


_spawned: list[subprocess.Popen[bytes]] = []
"""Handles for daemons this process started.

Kept only so that Popen objects are not garbage collected while the daemon is
still running, which would emit a ResourceWarning. The daemon is meant to
outlive us, so it is never waited on; Popen is simply not the right abstraction
for a process we deliberately do not manage.
"""


def spawn_daemon(endpoint: Endpoint) -> None:
    """Start a daemon in the background and return immediately.

    Args:
        endpoint: Where the daemon should listen.
    """
    command = [*_daemon_command(), "--endpoint", str(endpoint)]
    logger.info("starting a daemon: %s", " ".join(command))

    common: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # CREATE_NO_WINDOW, emphatically not DETACHED_PROCESS. Both are
        # described as freeing a child from the parent's console, and only one
        # of them does it quietly: measured on Windows 11, a console
        # application started with DETACHED_PROCESS has a *new* console
        # allocated for it, and that console is visible. It is the terminal
        # that used to flash up whenever a command started the service.
        # Adding CREATE_NO_WINDOW alongside does not help -- it is documented
        # as ignored in combination with DETACHED_PROCESS, and measurement
        # agrees.
        #
        # On its own, CREATE_NO_WINDOW leaves the daemon with no console
        # window of the parent's or of its own, so no console control event
        # can reach it -- including the one Windows sends when the terminal
        # that started it is closed. CREATE_NEW_PROCESS_GROUP keeps Ctrl+C out
        # as well. Shutting down deliberately goes through daemon.shutdown.
        child = subprocess.Popen(
            command,
            creationflags=subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP,
            **common,
        )
    else:
        # Its own session, so it survives this terminal closing.
        child = subprocess.Popen(command, start_new_session=True, **common)

    _spawned.append(child)


async def _enter_with_retry(stack: AsyncExitStack, endpoint: Endpoint) -> ByteStream:
    """Keep trying to connect until the daemon is up, or give up."""
    deadline = time.monotonic() + READY_TIMEOUT
    delay = _FIRST_DELAY
    while True:
        try:
            return await stack.enter_async_context(ipc.connect(endpoint))
        except _NOT_LISTENING:
            if time.monotonic() >= deadline:
                raise DaemonStartError(str(endpoint), paths.log_file()) from None
            await anyio.sleep(delay)
            delay = min(delay * _BACKOFF, _MAX_DELAY)


@asynccontextmanager
async def open_connection(
    endpoint: Endpoint, *, autostart: bool = True
) -> AsyncGenerator[ByteStream, None]:
    """Connect to the daemon, starting one if necessary.

    Args:
        endpoint: Where the daemon is, or should be.
        autostart: Whether to start a daemon when none is running.

    Yields:
        The connected stream.

    Raises:
        DaemonUnavailable: If no daemon is running and ``autostart`` is false.
        DaemonStartError: If a daemon was started but never became reachable.
    """
    async with AsyncExitStack() as stack:
        try:
            stream = await stack.enter_async_context(ipc.connect(endpoint))
        except _NOT_LISTENING:
            if not autostart:
                raise DaemonUnavailable(
                    f"the background service is not running ({endpoint})"
                ) from None
            logger.debug("no daemon at %s; starting one", endpoint)
            spawn_daemon(endpoint)
            stream = await _enter_with_retry(stack, endpoint)
        yield stream
