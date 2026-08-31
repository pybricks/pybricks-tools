# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Transport between the client and the service.

The user never types an address: :func:`pb_core.ipc.address.resolve` picks the
right one for the platform. Everything above this package deals only in
:class:`anyio.abc.ByteStream`, so neither the service nor the client contains any
platform-specific code.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from anyio.abc import ByteStream

from pb_core.ipc.address import ENDPOINT_ENV_VAR, Endpoint, default, resolve
from pb_core.ipc.backend import IpcBackend, IpcListener, StreamHandler, get_backend
from pb_core.ipc.errors import AddressInUseError, EndpointError

__all__ = [
    "ENDPOINT_ENV_VAR",
    "AddressInUseError",
    "Endpoint",
    "EndpointError",
    "IpcBackend",
    "IpcListener",
    "StreamHandler",
    "connect",
    "create_listener",
    "default",
    "get_backend",
    "resolve",
]


def connect(endpoint: Endpoint) -> AbstractAsyncContextManager[ByteStream]:
    """Connect to the service listening at ``endpoint``.

    Args:
        endpoint: Where the service is listening.

    Returns:
        An async context manager yielding the connected stream.

    Raises:
        ConnectionRefusedError: If nothing is listening. Callers treat this as
            "the service is not running" and may start one.
    """
    return get_backend(endpoint).connect(endpoint)


def create_listener(endpoint: Endpoint) -> AbstractAsyncContextManager[IpcListener]:
    """Listen for clients at ``endpoint``.

    Args:
        endpoint: Where to listen.

    Returns:
        An async context manager yielding the listener.

    Raises:
        AddressInUseError: If another service already holds the address.
    """
    return get_backend(endpoint).create_listener(endpoint)
