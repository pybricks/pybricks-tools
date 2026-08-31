# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Events a hub connection publishes to whoever is attached."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pb_service.hub.protocol import StatusReport

__all__ = [
    "AppDataEvent",
    "Channel",
    "HubEvent",
    "HubState",
    "StateEvent",
    "StatusEvent",
    "StdoutEvent",
    "StdoutRing",
    "channel_of",
]


class HubState(StrEnum):
    """Where a hub connection is in its life."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"


class Channel(StrEnum):
    """A stream of events a client can subscribe to independently.

    Separate because they have different costs: program output can be a flood,
    while state changes are rare and must not be lost behind it.
    """

    STDOUT = "stdout"
    STATUS = "status"
    APP_DATA = "app_data"
    STATE = "state"


@dataclass(frozen=True, slots=True)
class StdoutEvent:
    """Output from the running program."""

    seq: int
    data: bytes


@dataclass(frozen=True, slots=True)
class AppDataEvent:
    """Data the program sent to an app rather than to the terminal."""

    seq: int
    data: bytes


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """A status report from the hub."""

    seq: int
    report: StatusReport


@dataclass(frozen=True, slots=True)
class StateEvent:
    """The connection's state changed."""

    seq: int
    state: HubState
    reason: str | None = None


HubEvent = StdoutEvent | AppDataEvent | StatusEvent | StateEvent


def channel_of(event: HubEvent) -> Channel:
    """Return the channel an event belongs to.

    Args:
        event: The event.

    Returns:
        Its channel.
    """
    if isinstance(event, StdoutEvent):
        return Channel.STDOUT
    if isinstance(event, AppDataEvent):
        return Channel.APP_DATA
    if isinstance(event, StatusEvent):
        return Channel.STATUS
    return Channel.STATE


DEFAULT_SCROLLBACK_BYTES: Final = 256 * 1024


class StdoutRing:
    """Recent program output, kept whether or not anyone is listening.

    With a service that outlives commands, the usual sequence is to start a
    program in one window and attach a terminal in another moments later.
    Without a buffer that always loses the program's first lines.
    """

    def __init__(self, max_bytes: int = DEFAULT_SCROLLBACK_BYTES) -> None:
        """Create a ring buffer.

        Args:
            max_bytes: Total payload bytes to retain.
        """
        self._max_bytes = max_bytes
        self._chunks: deque[tuple[int, bytes]] = deque()
        self._size = 0
        self.oldest_seq = 0
        """Lowest sequence number still held, for reporting truncation."""

    def append(self, seq: int, data: bytes) -> None:
        """Record a chunk, discarding the oldest to stay within the limit.

        Args:
            seq: The chunk's sequence number.
            data: The chunk.
        """
        self._chunks.append((seq, data))
        self._size += len(data)
        while self._size > self._max_bytes and self._chunks:
            dropped_seq, dropped = self._chunks.popleft()
            self._size -= len(dropped)
            self.oldest_seq = dropped_seq + 1

    def since(self, seq: int) -> list[tuple[int, bytes]]:
        """Return retained chunks with a sequence number at or above ``seq``.

        Args:
            seq: The first sequence number wanted.

        Returns:
            The matching chunks, oldest first.
        """
        return [(s, data) for s, data in self._chunks if s >= seq]

    def tail(self, max_bytes: int) -> list[tuple[int, bytes]]:
        """Return the most recent chunks up to ``max_bytes``.

        Args:
            max_bytes: How much output to return.

        Returns:
            The matching chunks, oldest first.
        """
        selected: list[tuple[int, bytes]] = []
        total = 0
        for seq, data in reversed(self._chunks):
            if total + len(data) > max_bytes and selected:
                break
            selected.append((seq, data))
            total += len(data)
        selected.reverse()
        return selected

    @property
    def size(self) -> int:
        """Bytes currently retained."""
        return self._size
