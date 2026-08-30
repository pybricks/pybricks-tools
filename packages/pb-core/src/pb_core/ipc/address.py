# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Endpoint addressing for the daemon.

An endpoint is written as a URI so that one string can name any transport:

* ``unix:///run/user/1000/pybricks/pbd.sock`` -- AF_UNIX socket (Linux, macOS)
* ``pipe://pybricks-pbd-david`` -- Windows named pipe
* ``tcp://127.0.0.1:47823`` -- TCP, for remote daemons and for tests
* ``memory://name`` -- in-process, used by the test suite

Users are not expected to type these. :func:`resolve` picks the right default
for the platform, and ``PB_ENDPOINT`` overrides it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Literal

from pb_core import paths

__all__ = ["ENDPOINT_ENV_VAR", "Endpoint", "Scheme", "default", "resolve"]

ENDPOINT_ENV_VAR = "PB_ENDPOINT"

Scheme = Literal["unix", "pipe", "tcp", "memory"]


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A transport-independent address for the daemon."""

    scheme: Scheme
    target: str
    """Socket path, pipe name, ``host:port``, or in-process key."""

    @classmethod
    def parse(cls, uri: str) -> Endpoint:
        """Parse an endpoint URI.

        Args:
            uri: A string such as ``tcp://127.0.0.1:47823``.

        Returns:
            The parsed endpoint.

        Raises:
            ValueError: If the URI is malformed or names an unknown scheme.
        """
        scheme, separator, rest = uri.partition("://")
        if not separator:
            raise ValueError(f"endpoint is not a URI: {uri!r}")
        if scheme not in ("unix", "pipe", "tcp", "memory"):
            raise ValueError(f"unknown endpoint scheme: {scheme!r}")
        if not rest:
            raise ValueError(f"endpoint has no target: {uri!r}")
        if scheme == "tcp":
            host, colon, port = rest.rpartition(":")
            if not colon or not host:
                raise ValueError(f"tcp endpoint needs host:port: {uri!r}")
            try:
                number = int(port)
            except ValueError:
                raise ValueError(
                    f"tcp endpoint port is not a number: {uri!r}"
                ) from None
            if not 0 <= number <= 65535:
                raise ValueError(f"tcp endpoint port out of range: {uri!r}")
        return cls(scheme, rest)

    @property
    def tcp_address(self) -> tuple[str, int]:
        """Return ``(host, port)`` for a TCP endpoint.

        Raises:
            ValueError: If this is not a TCP endpoint.
        """
        if self.scheme != "tcp":
            raise ValueError(f"not a tcp endpoint: {self}")
        host, _, port = self.target.rpartition(":")
        return host, int(port)

    @property
    def is_loopback(self) -> bool:
        """Whether a TCP endpoint is confined to this machine.

        Non-loopback TCP carries the shared secret over a plaintext socket, so
        callers should require TLS or an SSH tunnel for it.
        """
        if self.scheme != "tcp":
            return True
        host, _ = self.tcp_address
        return host in ("127.0.0.1", "::1", "localhost")

    def __str__(self) -> str:
        """Render the endpoint back as a URI."""
        return f"{self.scheme}://{self.target}"


def default() -> Endpoint:
    """Return the platform's default endpoint.

    Windows uses a named pipe because asyncio cannot serve AF_UNIX sockets on
    the proactor event loop, which the daemon requires for other reasons.
    """
    if sys.platform == "win32":
        return Endpoint(
            "pipe", paths.windows_pipe_name().removeprefix(r"\.\pipe" + "\\")
        )
    return Endpoint("unix", str(paths.socket_path()))


def resolve(uri: str | None = None) -> Endpoint:
    """Return the endpoint to use.

    Precedence is the explicit argument, then ``PB_ENDPOINT``, then the
    platform default.

    Args:
        uri: An explicit endpoint URI, if the caller has one.

    Returns:
        The endpoint to connect to or listen on.
    """
    if uri:
        return Endpoint.parse(uri)
    from_env = os.environ.get(ENDPOINT_ENV_VAR)
    if from_env:
        return Endpoint.parse(from_env)
    return default()
