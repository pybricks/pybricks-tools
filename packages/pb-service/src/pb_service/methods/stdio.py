# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Carrying a hub's standard input and output to and from a client.

Three methods, and between them they are what an interactive terminal is made
of: ``hub.attach`` streams everything the hub says, ``hub.stdin`` sends
keystrokes back, and ``program.repl`` starts the interpreter that is listening.

**Attaching is a request that stays open**, sending notifications until the
caller cancels it. The shape is deliberate and matches ``hub.scan``: the
subscription lives inside one request, so it ends when the request does, and a
client that dies takes its subscription with it. Nothing has to be
unsubscribed by name, and there is no way to leak one.

**Writing to stdin is where the sharp edge is.** The firmware rejects a write
that does not fit its input buffer *whole*, with ``BUSY``, rather than keeping
the part that fits -- and how big that buffer is depends on the firmware. Newer
builds take as much as the Bluetooth link allows, which is what makes pasting a
block of code into the interpreter quick; older ones hold 21 bytes on a City,
Technic, Move or EV3 hub and 64 on a Prime, Essential or NXT, and nothing on
the wire says which this is. So a write starts at the size the hub's
capabilities allow, halves on refusal down to a floor, and waits and retries
from there. Treating a refusal as a failure instead would lose input exactly
when a program is slow to read, which is the case that matters most.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio

from pb_core.jsonrpc import (
    ErrorCode,
    JsonRpcError,
    JsonRpcPeer,
    decode_binary,
    encode_binary,
)
from pb_core.protocol import HUB_STATE, HUB_STATUS, HUB_STDOUT
from pb_service.errors import hub_errors
from pb_service.hub import protocol as p
from pb_service.hub.connection import HubConnection
from pb_service.hub.events import Channel, StateEvent, StatusEvent, StdoutEvent
from pb_service.methods import method, require
from pb_service.methods.hub import hub_of, report_json, status_json
from pb_service.session import Session

logger = logging.getLogger(__name__)

__all__ = ["hub_attach", "hub_stdin", "program_repl"]

STDIN_FLOOR = 20
"""The smallest write to fall back to when a hub keeps refusing.

Firmware that still holds 21 bytes of input takes 20 as long as it has read
anything at all, and typing arrives a keystroke at a time in any case. Nothing
smaller is ever worth trying: below this a refusal means the program is not
reading, not that the write was too big.
"""

STDIN_RETRY_SECONDS = 0.02
"""How long to wait before offering a busy hub the same bytes again.

Short, because what clears the condition is the program on the hub reading its
input, which takes a few milliseconds once it gets the chance.
"""

STDIN_PATIENCE_SECONDS = 10.0
"""How long to keep retrying before giving up on a hub that will not read.

A program that never reads its input leaves the buffer full for as long as it
runs, and waiting for it forever would hang the caller's keystroke with no
explanation. Ten seconds is far longer than any momentary busy period, and
short enough that the answer arrives while the user still remembers typing.
"""

ATTACH_CHANNELS = frozenset({Channel.STDOUT, Channel.STATUS, Channel.STATE})
"""What attaching subscribes to.

App data is deliberately absent: it is for a program talking to an application
rather than to a terminal, and putting it on the same stream would print binary
at somebody who asked for a REPL.
"""


def _peer(session: Session) -> JsonRpcPeer:
    """Return the connection to send notifications over.

    Args:
        session: The calling session.

    Returns:
        Its peer.

    Raises:
        JsonRpcError: If the session has no peer, which would leave the caller
            waiting for notifications that could never be sent.
    """
    if session.peer is None:  # pragma: no cover - a served session always has one
        raise JsonRpcError(ErrorCode.INTERNAL_ERROR, "this session cannot be notified")
    return session.peer


async def _offer(connection: HubConnection, chunk: bytes, profile: p.Version) -> bool:
    """Try to hand one piece of input to the hub.

    Args:
        connection: The hub to write to.
        chunk: The bytes to offer.
        profile: The hub's protocol version, which decides the encoding.

    Returns:
        True if the hub took it, False if it said it was busy -- which means
        the bytes did not fit and none of them were kept.

    Raises:
        JsonRpcError: If the hub refused for any other reason, or cannot take
            input at all.
    """
    try:
        command = p.encode_write_stdin(chunk, profile=profile)
    except ValueError as exc:
        raise JsonRpcError(ErrorCode.HUB_UNSUPPORTED, str(exc)) from exc

    try:
        with hub_errors("accept that input"):
            await connection.send_command(command)
    except JsonRpcError as exc:
        if exc.code != ErrorCode.HUB_BUSY:
            raise
        return False
    return True


async def _write_stdin(connection: HubConnection, data: bytes) -> int:
    """Hand bytes to the hub, in pieces it will accept.

    The hub answers ``BUSY`` both when a write does not fit its input buffer at
    all and when the buffer is merely full at that moment, and nothing
    distinguishes the two. So a refusal first makes the write smaller, down to
    a floor below which the only remaining explanation is a program that is not
    reading, and from there it waits and tries again. What that size turns out
    to be is remembered on the connection, so a hub is measured once rather
    than once per keystroke.

    Args:
        connection: The hub to write to.
        data: What to send.

    Returns:
        How many bytes were accepted, which is all of them or none.

    Raises:
        JsonRpcError: If the hub is not ready, or stayed busy for longer than
            its patience allows.
    """
    info = connection.info
    if info is None:
        raise JsonRpcError(ErrorCode.HUB_NOT_CONNECTED, "hub is not ready")

    offset = 0
    with anyio.move_on_after(STDIN_PATIENCE_SECONDS) as patience:
        while offset < len(data):
            limit = max(1, connection.stdin_limit)
            chunk = data[offset : offset + limit]
            if await _offer(connection, chunk, info.profile):
                offset += len(chunk)
                continue

            if connection.stdin_limit > STDIN_FLOOR:
                connection.stdin_limit = max(STDIN_FLOOR, connection.stdin_limit // 2)
                logger.info(
                    "hub takes less input than it offered; using %d bytes a write",
                    connection.stdin_limit,
                )
                continue

            # As small as it goes and still refused, so the hub is simply not
            # reading yet. Wait for the program to catch up.
            await anyio.sleep(STDIN_RETRY_SECONDS)

    if patience.cancelled_caught:
        raise JsonRpcError(
            ErrorCode.HUB_BUSY,
            "the hub is not reading its input",
            {"waited_s": STDIN_PATIENCE_SECONDS, "written": offset},
        )
    return offset


@method(
    "hub.attach",
    "Stream a hub's output, status and connection state until cancelled. "
    "Everything arrives as hub.stdout, hub.status and hub.state notifications; "
    "the request itself returns only when the hub disconnects.",
    params={
        "id": "The hub, by name or id. Defaults to the current hub.",
        "replay_bytes": (
            "How much recent output to deliver first, for a client attaching "
            "after a program has already started. Zero for none."
        ),
    },
    returns="Why the stream ended, and how many events this client missed.",
)
async def hub_attach(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Attach to a hub's stdio."""
    session.service.mark_activity()
    connection = hub_of(session, params)
    peer = _peer(session)
    hub_id = connection.id
    replay = int(params.get("replay_bytes") or 0)
    dropped = 0

    async with connection.subscribe(ATTACH_CHANNELS, replay_bytes=replay) as subscriber:
        # The status as it stands, before anything about it changes. Without
        # this, a client would have nothing to draw a status line from until
        # the hub next changed, which on an idle hub is a long time to wait.
        peer.notify(HUB_STATUS, {"id": hub_id} | status_json(connection))

        async for event in connection.events(subscriber):
            if isinstance(event, StdoutEvent):
                peer.notify(
                    HUB_STDOUT,
                    {"id": hub_id, "seq": event.seq, "data": encode_binary(event.data)},
                )
            elif isinstance(event, StatusEvent):
                peer.notify(
                    HUB_STATUS,
                    {"id": hub_id, "seq": event.seq} | report_json(event.report),
                )
            elif isinstance(event, StateEvent):
                peer.notify(
                    HUB_STATE,
                    {
                        "id": hub_id,
                        "seq": event.seq,
                        "state": str(event.state),
                        "reason": event.reason,
                    },
                )

        dropped = subscriber.dropped

    logger.info("stdio stream ended (%d event(s) dropped)", dropped)
    return {"reason": "disconnected", "dropped": dropped}


@method(
    "hub.stdin",
    "Send input to the program running on a hub.",
    params={
        "id": "The hub, by name or id. Defaults to the current hub.",
        "data": "The bytes to send, base64 encoded.",
    },
    returns="How many bytes were accepted.",
)
async def hub_stdin(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Write to a hub's standard input."""
    session.service.mark_activity()
    connection = hub_of(session, params)
    data = decode_binary(require(params, "data", str))
    return {"size": await _write_stdin(connection, data)}


@method(
    "program.repl",
    "Start the hub's built-in interactive interpreter.",
    params={"id": "The hub, by name or id. Defaults to the current hub."},
    returns="An empty object.",
)
async def program_repl(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Start the REPL."""
    session.service.mark_activity()
    connection = hub_of(session, params)
    info = connection.info
    if info is None:
        raise JsonRpcError(ErrorCode.HUB_NOT_CONNECTED, "hub is not ready")

    if not info.capabilities.flags & p.FeatureFlag.BUILTIN_USER_PROGRAM_REPL:
        # Asked before sending, because a hub without a REPL refuses with the
        # same INVALID_COMMAND it gives for an empty program slot, and the two
        # want very different advice.
        raise JsonRpcError(
            ErrorCode.HUB_UNSUPPORTED,
            "this hub's firmware has no interactive interpreter",
        )

    try:
        command = p.encode_start_repl(profile=info.profile)
    except ValueError as exc:
        raise JsonRpcError(ErrorCode.HUB_UNSUPPORTED, str(exc)) from exc

    with hub_errors("start the interpreter"):
        await connection.send_command(command)
    logger.info("REPL started")
    return {}
