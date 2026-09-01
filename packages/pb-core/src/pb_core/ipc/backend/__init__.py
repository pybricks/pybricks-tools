# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Per-transport backends, selected by endpoint scheme."""

from __future__ import annotations

import sys

from pb_core.ipc.address import Endpoint
from pb_core.ipc.backend.provider import IpcBackend, IpcListener, StreamHandler

__all__ = ["IpcBackend", "IpcListener", "StreamHandler", "get_backend"]


def get_backend(endpoint: Endpoint) -> IpcBackend:
    """Return the backend that implements ``endpoint``.

    Backends are imported lazily so that, for example, importing the client on
    Linux never touches the Windows pipe module.

    Args:
        endpoint: The endpoint to be connected to or served.

    Returns:
        A backend for that endpoint's scheme.

    Raises:
        NotImplementedError: If the scheme is not supported on this platform.
    """

    if endpoint.scheme == "memory":
        from pb_core.ipc.backend.memory import MemoryBackend

        return MemoryBackend()

    if endpoint.scheme == "tcp":
        from pb_core.ipc.backend.tcp import TcpBackend

        return TcpBackend()

    if endpoint.scheme == "pipe":
        if sys.platform != "win32":
            raise NotImplementedError("named pipes are only available on Windows")

        from pb_core.ipc.backend.windows import WindowsPipeBackend

        return WindowsPipeBackend()

    if sys.platform == "win32":
        raise NotImplementedError(
            "asyncio cannot serve AF_UNIX sockets on the Windows proactor loop; "
            "use a pipe:// endpoint"
        )

    from pb_core.ipc.backend.posix import PosixBackend

    return PosixBackend()
