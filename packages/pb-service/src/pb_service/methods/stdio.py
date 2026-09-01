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
that does not fit the free space in its input buffer *whole*, with ``BUSY``,
rather than keeping the part that fits. So a write is offered at the size the
hub's capabilities allow and retried at that size until it is taken. Treating a
refusal as a failure instead would lose input exactly when a program is slow to
read, which is the case that matters most.

**What a refusal means is that nothing on the hub is reading**, and that is the
whole of it. The buffer is emptied only by whatever is on the other end: the
interpreter drains it while it sits at a prompt, and not while it is running a
line; a user program drains it only as often as it reads ``sys.stdin``. So a
hub that keeps saying ``BUSY`` is a hub with nothing reading, and the remedy is
on the hub rather than here -- which is what :data:`STDIN_PATIENCE_SECONDS`
eventually says.

**Nothing here adapts the write size, and that is deliberate.** It used to
halve on a refusal that lasted, on the theory that a long refusal meant the
buffer was smaller than advertised, remembering where it settled. The theory
does not hold: the firmware's test is
``pbsys_host_stdin_get_free() < size - 1``, which is free space, and free space
is reduced by whatever the reader has not read. A smaller write being accepted
therefore says only that a little room happened to be left, not that the buffer
is small -- so no sequence of writes can measure the buffer without knowing it
is empty, which nothing reports. Measured on a City hub, the adaptive version
walked the write size from 479 bytes to a 20-byte floor after one program that
ignored its input, and nothing put it back for the life of the connection: a
four-hundred-byte paste went from 73 ms to 2589 ms.
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

STDIN_RETRY_SECONDS = 0.03
"""How long to wait before offering a busy hub the same bytes again.

**One Bluetooth connection interval**, because that is the soonest the answer
can possibly be different. How quickly the program on the hub reads its input
is the wrong quantity to size this by, and is what the value here was first
argued from: the write is a GATT write with response, so an attempt is a round
trip, and a different answer cannot reach us inside one connection event
however promptly the hub acts.

Asking more often is not free either. A write occupies a connection event that
would otherwise carry a notification, and notifications are how the hub's
output reaches us -- so retrying harder competes with the reading that is the
only thing which can make room.

**Which interval, exactly, is not something the host can see.** The firmware
asks for 30 ms to 60 ms on connection -- ``intervalMin = 24`` and
``intervalMax = 48``, in units of 1.25 ms, in ``bluetooth_stm32_cc2640.c`` --
and the btstack hubs ask for nothing at all, so in both cases the negotiated
value belongs to the host's Bluetooth stack and is not reported through bleak.
Measurement says it can be quicker than the request: 479 bytes went to a City
hub as 20-byte writes in 0.45 s, which is about 19 ms for each write and its
answer. So the bottom of the requested range is the value to take. Pacing to
the top of it would be slower than the link rather than matched to it, and this
runs while somebody is pasting.

A retry rather than a poll of something idle: nothing on the hub reports free
space, so there is no event to wait for, and this runs only while a write is
outstanding.
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
    """Hand bytes to the hub, at the size it says it takes.

    Cut into :attr:`HubConnection.stdin_limit` pieces, and each piece offered
    over and over at that size until the hub takes it. **The size never
    changes**, because a refusal carries no information about it: the firmware
    tests the buffer's *free space*, which is whatever the reader on the hub has
    not read yet, so a refusal says only that nothing has read lately. See the
    module docstring for the version of this that tried to be clever and what it
    cost.

    Retries are paced to :data:`STDIN_RETRY_SECONDS` and bounded by
    :data:`STDIN_PATIENCE_SECONDS`, after which the caller is told the truth --
    that nothing on the hub is reading -- rather than being left holding a
    keystroke.

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
    limit = connection.stdin_limit

    with anyio.move_on_after(STDIN_PATIENCE_SECONDS) as patience:
        while offset < len(data):
            if await _offer(connection, data[offset : offset + limit], info.profile):
                offset += min(limit, len(data) - offset)

                continue

            # Nothing on the hub has read its input yet. There is nothing to
            # conclude and nothing to adjust, so wait for the soonest moment
            # the answer could be different and ask again.
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
