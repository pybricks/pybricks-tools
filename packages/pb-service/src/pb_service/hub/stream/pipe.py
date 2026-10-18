# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The little of an open byte stream a hub link uses.

Two methods, because that is all the framing needs. Everything else about a
particular carrier -- the baud rate and modem pins of a cable, the address and
channel of a radio -- belongs to opening one, and so lives with that transport
rather than here.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

__all__ = ["BytePipe", "PipeOpener"]


class BytePipe(Protocol):
    """An open byte stream, readable and writable."""

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        """Read up to and including the separator.

        Args:
            separator: The byte sequence to stop after.

        Returns:
            Everything up to and including it.
        """
        ...

    async def write(self, data: bytes | bytearray | memoryview) -> None:
        """Send bytes, waiting for them to leave the application buffer.

        Args:
            data: What to send.
        """
        ...


PipeOpener = Callable[[str], AbstractAsyncContextManager[BytePipe]]
"""How a pipe is opened, given the name of what is on the other end."""
