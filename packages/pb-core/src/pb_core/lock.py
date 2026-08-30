# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Single-instance locking for the daemon.

Two clients may try to start a daemon at the same moment. Rather than
coordinating, both are allowed to spawn: one wins the lock, the loser exits, and
both clients' readiness loops then connect to the winner.

An advisory file lock is used rather than a PID file because the kernel releases
it when the process dies, however it dies. A PID file has to be validated
against a process that may have exited, or worse, whose id has been reused.

The lock also makes it safe to remove a stale AF_UNIX socket: holding it proves
no other daemon is serving, so any socket left on disk is debris from a crash.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from pb_core import paths

logger = logging.getLogger(__name__)

__all__ = ["AlreadyRunningError", "single_instance"]


class AlreadyRunningError(RuntimeError):
    """Another daemon already holds the lock."""


def _try_lock(handle: IO[str]) -> bool:
    """Take an exclusive non-blocking lock, reporting whether it succeeded.

    Both implementations lock relative to the current file position, so the
    caller must seek to the start first.
    """
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[str]) -> None:
    """Release the lock, ignoring failures during teardown."""
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.lockf(handle.fileno(), fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - teardown only
        logger.debug("could not release the lock file cleanly", exc_info=True)


@contextmanager
def single_instance(path: Path | None = None) -> Generator[Path, None, None]:
    """Hold the daemon's single-instance lock for the duration of the block.

    This is a synchronous context manager on purpose. Releasing the lock must
    not be cancellable, and a synchronous ``__exit__`` cannot be cancelled at
    all, which is stronger than shielding.

    Args:
        path: Override the lock file location.

    Yields:
        The lock file path.

    Raises:
        AlreadyRunningError: If another daemon holds the lock.
    """
    lock_path = path or paths.lock_file()
    paths.ensure_private_dir(lock_path.parent)

    handle = lock_path.open("a+", encoding="ascii")
    try:
        # msvcrt locks a single byte, so the file must not be empty. The first
        # byte is reserved for the lock and never carries information.
        if lock_path.stat().st_size == 0:
            handle.write("\n")
            handle.flush()

        if not _try_lock(handle):
            raise AlreadyRunningError(f"another daemon already holds {lock_path}")

        # Written after the lock is held, and past the locked byte. The pid is
        # only ever diagnostic; the lock itself provides mutual exclusion.
        handle.seek(1)
        handle.truncate(1)
        handle.write(str(os.getpid()))
        handle.flush()
        logger.debug("holding single-instance lock %s", lock_path)

        try:
            yield lock_path
        finally:
            _unlock(handle)
    finally:
        handle.close()
