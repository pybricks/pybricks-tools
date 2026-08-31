# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The interface every IPC transport implements.

Everything above this layer deals only in :class:`anyio.abc.ByteStream`, so the
service and the client contain no platform-specific code at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from typing import Any

from anyio.abc import ByteStream, TaskGroup

from pb_core.ipc.address import Endpoint

__all__ = ["IpcBackend", "IpcListener", "StreamHandler", "owned"]


def owned(handler: StreamHandler) -> StreamHandler:
    """Wrap ``handler`` so the accepted stream is closed when it returns.

    The listener owns every stream it accepts. Backends differ in whether
    their underlying machinery closes streams for them, so every backend
    routes handlers through this to guarantee one contract: when the handler
    returns, the stream is closed. Without it, connection handling leaks.

    Args:
        handler: The per-connection handler to wrap.

    Returns:
        An equivalent handler that closes the stream afterwards.
    """

    async def wrapper(stream: ByteStream) -> None:
        async with stream:
            await handler(stream)

    return wrapper


StreamHandler = Callable[[ByteStream], Coroutine[Any, Any, None]]
"""Called once per accepted connection, inside the caller's task group."""


class IpcListener(ABC):
    """A listening endpoint that accepts client connections."""

    @abstractmethod
    async def serve(self, handler: StreamHandler, task_group: TaskGroup) -> None:
        """Accept connections until cancelled, running ``handler`` for each.

        The task group is required rather than optional: every connection task
        must have an owner whose lifetime the caller controls.

        The accepted stream is closed once ``handler`` returns.

        Args:
            handler: Coroutine function invoked with each accepted stream.
            task_group: The group that will own the per-connection tasks.
        """

    @abstractmethod
    async def aclose(self) -> None:
        """Stop listening and release the underlying resources."""


class IpcBackend(ABC):
    """Creates listeners and connections for one family of transports."""

    @abstractmethod
    def connect(self, endpoint: Endpoint) -> AbstractAsyncContextManager[ByteStream]:
        """Connect to a service.

        Args:
            endpoint: Where the service is listening.

        Returns:
            An async context manager yielding the connected stream.

        Raises:
            ConnectionRefusedError: If no service is listening there. Callers
                treat this as "not running" and may start one.
        """

    @abstractmethod
    def create_listener(
        self, endpoint: Endpoint
    ) -> AbstractAsyncContextManager[IpcListener]:
        """Begin listening for clients.

        Args:
            endpoint: Where to listen.

        Returns:
            An async context manager yielding the listener.

        Raises:
            OSError: If the address is already in use by another service.
        """
