# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Fixtures that apply to every test in the workspace.

There is one, and it is here rather than in a package because the problem it
solves crosses packages.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

_QUIET_LOGGERS = ("bleak", "asyncio")
"""The loggers ``setup_logging`` holds at a higher level, and so must restore."""


@pytest.fixture(autouse=True)
def _leave_logging_as_it_was() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Undo any logging a test set up, before the next one runs.

    Every ``pb`` command configures logging as its first act, which adds a
    handler to the root logger pointing at whatever ``sys.stderr`` was at the
    time. Under pytest that is the capture for the test being run, and it is
    closed when that test ends -- so the handler outlives the stream it writes
    to, and the next test that logs from a background task dies with
    ``ValueError: I/O operation on closed file`` in a place that has nothing to
    do with the test that installed it.

    That is exactly what happened: adding tests for ``pb service log`` broke a
    hub test three packages away, and only on the run where both were
    collected. A leaked handler is a global, so it gets cleaned up globally.

    Yields:
        Nothing. Root handlers and levels are put back on the way out.
    """
    root = logging.getLogger()
    quiet = [logging.getLogger(name) for name in _QUIET_LOGGERS]
    handlers = root.handlers[:]
    level = root.level
    quiet_levels = [(logger, logger.level) for logger in quiet]

    yield

    root.handlers[:] = handlers
    root.setLevel(level)
    for logger, was in quiet_levels:
        logger.setLevel(was)
