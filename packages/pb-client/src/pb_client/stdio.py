# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What a hub says while a program is running on it.

Three kinds of thing arrive on one stream, and keeping them on one stream is
the point: a caller drawing a terminal has to show output and status in the
order they happened, and merging two streams afterwards cannot recover an order
that was thrown away. So the daemon sends them down one connection in order,
and they are parsed here into one union.

Output is :class:`bytes` and stays :class:`bytes` all the way from the hub.
A chunk can end in the middle of a multi-byte character, so anything that
decodes on the way through corrupts it; only the end that owns a terminal is in
a position to decode, and it needs an incremental decoder to do it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from anyio.streams.memory import MemoryObjectReceiveStream

from pb_core.jsonrpc import decode_binary
from pb_core.protocol import HUB_STATE, HUB_STATUS, HUB_STDOUT

__all__ = ["REPL_PROGRAM_ID", "HubEvent", "Output", "State", "Status", "events"]

REPL_PROGRAM_ID = 128
"""The program id the hub reports while its interpreter is what is running.

Worth naming on this side because it is the difference between a status line
that says "REPL" and one that says "program running", which is what a user
looks at to know whether their typing is going anywhere.
"""


@dataclass(frozen=True, slots=True)
class Output:
    """A chunk of what the program on the hub printed."""

    seq: int
    """Where this came in the hub's stream, for resuming without a gap."""

    data: bytes
    """The bytes as the hub sent them, undecoded."""


@dataclass(frozen=True, slots=True)
class Status:
    """What the hub currently reports about itself.

    A level rather than an event: each one carries the whole status, so a
    client that misses one and catches the next is still correct.
    """

    seq: int
    flags: int
    names: tuple[str, ...]
    """The set bits by name, such as ``BATTERY_LOW_VOLTAGE_WARNING``."""

    program_running: bool
    running_program_id: int | None
    selected_slot: int | None

    @property
    def repl_running(self) -> bool:
        """Whether what is running is the hub's own interpreter."""
        return self.program_running and self.running_program_id == REPL_PROGRAM_ID


@dataclass(frozen=True, slots=True)
class State:
    """The hub's connection changed state."""

    seq: int
    state: str
    reason: str | None = None

    @property
    def connected(self) -> bool:
        """Whether the hub is still there."""
        return self.state == "connected"


HubEvent = Output | Status | State


def _parse(method: str, params: dict[str, Any]) -> HubEvent | None:
    """Turn one notification into an event.

    Args:
        method: The notification's name.
        params: Its parameters.

    Returns:
        The event, or ``None`` for a notification this build does not know --
        a daemon can be newer than its client, and an unknown event is not an
        error, it is just not for us.
    """
    seq = int(params.get("seq") or 0)
    if method == HUB_STDOUT:
        return Output(seq=seq, data=decode_binary(str(params.get("data", ""))))
    if method == HUB_STATUS:
        names = params.get("status_names") or []
        running = params.get("running_program_id")
        slot = params.get("selected_slot")
        return Status(
            seq=seq,
            flags=int(params.get("status_flags") or 0),
            names=tuple(str(name) for name in names),
            program_running=bool(params.get("program_running")),
            running_program_id=int(running) if isinstance(running, int) else None,
            selected_slot=int(slot) if isinstance(slot, int) else None,
        )
    if method == HUB_STATE:
        reason = params.get("reason")
        return State(
            seq=seq,
            state=str(params.get("state", "unknown")),
            reason=str(reason) if reason else None,
        )
    return None


async def events(
    payloads: MemoryObjectReceiveStream[tuple[str, dict[str, Any]]],
) -> AsyncIterator[HubEvent]:
    """Parse a hub's notifications as they arrive.

    Args:
        payloads: The raw notifications, each with the method that carried it.

    Yields:
        One event per notification this build understands, in the order the
        hub produced them.
    """
    async with payloads:
        async for method, params in payloads:
            event = _parse(method, params)
            if event is not None:
                yield event
