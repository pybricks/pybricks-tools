# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Shared-secret authentication between the client and the service.

The service writes a random token to a file only the current user can read, and
every client presents it in the mandatory first call. This is the X11
magic-cookie scheme.

It is required on every transport rather than only on TCP. The default
discretionary access control list on a Windows named pipe grants Everyone read
access, so without a secret another local user could not issue commands but
could still watch a hub's output. It is not sufficient on its own for a service
reachable beyond loopback, where the token crosses the network in clear and
needs TLS or an SSH tunnel around it.
"""

from __future__ import annotations

import logging
import secrets
import sys
from pathlib import Path

from pb_core import paths

logger = logging.getLogger(__name__)

__all__ = ["TOKEN_BYTES", "load_token", "read_or_create_token"]

TOKEN_BYTES = 32


def read_or_create_token(path: Path | None = None) -> str:
    """Return the service's token, creating one if there is none.

    Called by the service at startup.

    Args:
        path: Override the cookie file location.

    Returns:
        The token as hexadecimal text.
    """
    cookie = path or paths.cookie_file()
    paths.ensure_private_dir(cookie.parent)

    if cookie.exists():
        existing = cookie.read_text(encoding="ascii").strip()
        if existing:
            return existing

    token = secrets.token_hex(TOKEN_BYTES)
    # Create with restrictive permissions from the outset rather than widening
    # then narrowing, which would leave a window where the token is readable.
    descriptor = cookie.open("w", encoding="ascii")
    try:
        if sys.platform != "win32":
            import os

            os.fchmod(descriptor.fileno(), 0o600)
        descriptor.write(token)
    finally:
        descriptor.close()
    logger.debug("wrote a new authentication token to %s", cookie)
    return token


def load_token(path: Path | None = None) -> str | None:
    """Return the service's token if it can be read.

    Called by clients before connecting.

    Args:
        path: Override the cookie file location.

    Returns:
        The token, or ``None`` if no service has ever run for this user.
    """
    cookie = path or paths.cookie_file()
    try:
        token = cookie.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return token or None
