# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Transport errors normalised across backends.

Each platform reports "something is already listening there" differently: a
``PermissionError`` from ``FILE_FLAG_FIRST_PIPE_INSTANCE`` on Windows named
pipes, ``EADDRINUSE`` from a socket bind, ``WSAEADDRINUSE`` on Windows TCP.
The service needs one signal meaning "another service already holds this
endpoint", so backends raise :class:`AddressInUseError` instead of leaving
callers to match on OS error strings.
"""

from __future__ import annotations

import errno

__all__ = ["AddressInUseError", "EndpointError", "is_address_in_use"]

_WSAEADDRINUSE = 10048


class EndpointError(ValueError):
    """An endpoint URI is malformed or names an unknown transport.

    Almost always a typo in ``PB_ENDPOINT``. It is named rather than a plain
    :class:`ValueError` so that a command line can tell a mistyped address --
    which the user can fix -- from a bug, which it must not pretend to
    understand. A subclass of :class:`ValueError` so that existing handling
    keeps working.
    """


class AddressInUseError(OSError):
    """Another process is already listening on this endpoint.

    A subclass of :class:`OSError` so that existing ``except OSError`` handling
    keeps working.
    """


def is_address_in_use(exc: OSError) -> bool:
    """Whether ``exc`` reports that an address is already taken.

    Args:
        exc: The error raised while binding.

    Returns:
        ``True`` if this is an address-in-use failure on any platform.
    """
    if exc.errno == errno.EADDRINUSE:
        return True
    return getattr(exc, "winerror", None) == _WSAEADDRINUSE
