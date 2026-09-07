# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Fixtures shared by the end-to-end tests.

There is one, and it exists because ``sockaddr_un.sun_path`` is short.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def short_tmp_path() -> Iterator[Path]:
    """A temporary directory an AF_UNIX socket path still fits inside.

    ``sun_path`` holds 104 bytes on Darwin and 108 on Linux, counting the
    terminator, and pytest's ``tmp_path`` is nowhere near short enough on
    macOS. On a macOS runner the per-user temporary directory alone is 57
    bytes -- ``/private/var/folders`` plus two generated names -- and pytest
    adds its own two levels and the name of the test, which took the sockets
    these tests bind to 132 bytes and failed every one of them with
    ``OSError: AF_UNIX path too long``.

    Shortening pytest's own name for the directory does not buy enough room:
    the same path with a two-character name still measures 103 bytes, and the
    service's socket inside a ``runtime`` directory measures 109. So the
    directory is made somewhere genuinely short instead.

    Use this, rather than ``tmp_path``, for anything that becomes a socket
    path or is handed to the service as ``PB_RUNTIME_DIR``. Ordinary files a
    test reads and writes should stay on ``tmp_path``, which is named after
    the test and kept when one fails.

    Yields:
        The directory, removed on the way out. ``mkdtemp`` creates it 0700,
        which the service requires of a runtime directory on POSIX.
    """

    # /tmp is the shortest directory POSIX guarantees, and length is the whole
    # point here. On Windows the service is reached through a named pipe, so
    # nothing is measured against sun_path and the default is fine.
    root = None if sys.platform == "win32" else "/tmp"
    directory = Path(tempfile.mkdtemp(prefix="pb-e2e-", dir=root))

    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
