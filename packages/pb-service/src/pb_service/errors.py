# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Turning a hub failure into the wire error that describes it.

Anything that reaches the dispatcher untranslated becomes ``INTERNAL_ERROR``,
which tells a user only that something went wrong and leaves the real reason in
a log they have to be told to go and read. That is the worst outcome for the
most ordinary situations -- a hub that is busy, or asked to start a program it
does not have -- so every failure a hub can produce is named here.

The raw ``pbio_pybricks_error_t`` value travels in ``error.data``, because the
client should be able to be exact about what the firmware said rather than
paraphrasing a paraphrase.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_service.hub import protocol as p
from pb_service.hub.connection import HubDisconnectedError
from pb_service.hub.manager import HubAlreadyConnectedError, HubNotFoundError
from pb_service.hub.transport import HubCommandError

logger = logging.getLogger(__name__)

__all__ = ["HUB_FAILURES", "as_wire_error", "hub_errors"]

HUB_FAILURES = (
    HubCommandError,
    HubDisconnectedError,
    HubNotFoundError,
    HubAlreadyConnectedError,
)
"""Everything a hub can fail with that has a wire error of its own."""


def _refusal(exc: HubCommandError, doing: str | None) -> JsonRpcError:
    """Describe a command the hub said no to."""
    code = (
        ErrorCode.HUB_BUSY
        if exc.error == p.HubError.BUSY
        else ErrorCode.HUB_INVALID_COMMAND
    )
    name = exc.error.name if isinstance(exc.error, p.HubError) else None
    data: dict[str, Any] = {"hub_error": int(exc.error), "hub_error_name": name}
    # "hub refused the command: INVALID_COMMAND" is true and useless. Where the
    # caller said what it was attempting, say that instead.
    message = f"the hub would not {doing}" if doing else str(exc)
    # Worth a line in the log even though the client is told: which code the
    # firmware chose is the difference between "nothing is stored there" and
    # "that program is not valid", and only the log keeps it.
    logger.info("hub refused: %s (%s)", message, name or f"{int(exc.error):#04x}")
    return JsonRpcError(code, message, data)


def as_wire_error(exc: Exception, doing: str | None = None) -> JsonRpcError:
    """Translate a hub failure into the error to send back.

    Args:
        exc: What went wrong.
        doing: What was being attempted, phrased to follow "the hub would not"
            -- for example ``"start that program"``. Used only for a refusal,
            where the firmware's own wording is a bare error name.

    Returns:
        The equivalent :class:`JsonRpcError`. Anything unrecognised becomes
        ``INTERNAL_ERROR``, which is the honest answer for a bug.
    """

    if isinstance(exc, HubCommandError):
        return _refusal(exc, doing)

    if isinstance(exc, HubNotFoundError):
        return JsonRpcError(ErrorCode.HUB_NOT_FOUND, str(exc))

    if isinstance(exc, HubAlreadyConnectedError):
        return JsonRpcError(ErrorCode.HUB_ALREADY_CONNECTED, str(exc))

    if isinstance(exc, HubDisconnectedError):
        return JsonRpcError(ErrorCode.HUB_DISCONNECTED, str(exc))

    logger.error("unexpected hub failure", exc_info=exc)
    return JsonRpcError(ErrorCode.INTERNAL_ERROR, str(exc))


@contextmanager
def hub_errors(doing: str | None = None) -> Generator[None]:
    """Translate any hub failure raised inside the block.

    Args:
        doing: What is being attempted, as for :func:`as_wire_error`.

    Yields:
        Nothing; this only shapes what escapes.
    """

    try:
        yield
    except HUB_FAILURES as exc:
        raise as_wire_error(exc, doing) from exc
