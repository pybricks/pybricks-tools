# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Fixtures that apply to every test in the workspace.

They are here rather than in a package because the problems they solve cross
packages.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

import pb_service.methods.hub

GRACE_SECONDS = 0.05
"""How long a connect waits after the first hub it hears, under test.

Long enough for every simulated hub to be heard, since they are all heard at
once, and short enough not to add up.
"""

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


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Stop a connect waiting for hubs that have all been heard already.

    A connect that finds a hub waits a moment for a better route to it, since
    real hubs advertise only every so often. Simulated hubs are all heard at
    once, so under test the wait is only ever waited out, by every connect that
    is not by id -- which was more than a third of the time the whole suite
    took. A test about the wait itself sets it back.
    """
    monkeypatch.setattr(pb_service.methods.hub, "PREFERENCE_SECONDS", GRACE_SECONDS)
