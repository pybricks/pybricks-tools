# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

import contextvars
import io
import logging
from collections.abc import Iterator

import pytest

from pb_core import logging as pb_logging


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """Install a plain handler writing to a buffer, and remove it afterwards."""
    stream = io.StringIO()
    handler = pb_logging.setup_logging(
        level=pb_logging.TRACE, style="plain", stream=stream, quiet_loggers=()
    )
    try:
        yield stream
    finally:
        logging.getLogger().removeHandler(handler)


def test_trace_is_below_debug() -> None:
    assert pb_logging.TRACE < logging.DEBUG
    assert logging.getLevelName(pb_logging.TRACE) == "TRACE"


def test_context_fields_appear_in_records(captured: io.StringIO) -> None:
    log = logging.getLogger("pb_core.test")
    with pb_logging.log_context(hub="ble:AA:BB"):
        log.info("connected")
    assert "hub=ble:AA:BB" in captured.getvalue()
    assert "connected" in captured.getvalue()


def test_context_nests_and_restores(captured: io.StringIO) -> None:
    log = logging.getLogger("pb_core.test")
    with pb_logging.log_context(client="pb/1"):
        with pb_logging.log_context(hub="h1"):
            log.info("inner")
        log.info("outer")
    lines = captured.getvalue().splitlines()
    assert "client=pb/1" in lines[0] and "hub=h1" in lines[0]
    assert "client=pb/1" in lines[1] and "hub=h1" not in lines[1]


def test_no_context_renders_cleanly(captured: io.StringIO) -> None:
    logging.getLogger("pb_core.test").info("bare")
    assert "[" not in captured.getvalue().split("bare")[0].split("pb_core.test")[-1]


def test_elide_truncates_long_payloads() -> None:
    assert pb_logging.elide("x" * 10, limit=32) == "x" * 10
    out = pb_logging.elide("x" * 100, limit=10)
    assert out.startswith("x" * 10)
    assert "100 chars" in out


def test_quiet_loggers_are_held_above_level() -> None:
    handler = pb_logging.setup_logging(
        level=logging.DEBUG, style="null", quiet_loggers=("noisy_dep",)
    )
    try:
        assert logging.getLogger("noisy_dep").level > logging.DEBUG
    finally:
        logging.getLogger().removeHandler(handler)


def test_a_context_left_in_another_context_does_not_raise(
    captured: io.StringIO,
) -> None:
    """Entered in one context, left in another, which really happens.

    A hub connection is handed over with ``task_group.start()``, and one
    failed connect left the block in a different context from the one it was
    entered in. Resetting a token then raises -- from ``__exit__``, so it
    replaces whatever error was already on its way out, and a hub that would
    not answer its GATT request was reported to the user as an unexpected
    fault in the service.
    """
    block = pb_logging.log_context(hub="ble:AA")
    contextvars.copy_context().run(block.__enter__)

    assert block.__exit__(None, None, None) is not True

    # And the fields really are gone from the context it was left in, so the
    # next hub's logs cannot inherit this one's name.
    logging.getLogger(__name__).info("after")
    assert "hub=ble:AA" not in captured.getvalue()


def test_a_context_restores_what_was_there_before(captured: io.StringIO) -> None:
    with pb_logging.log_context(client="c1"):
        with pb_logging.log_context(hub="ble:AA"):
            pass
        logging.getLogger(__name__).info("still the client")
    assert "[client=c1] still the client" in captured.getvalue()
