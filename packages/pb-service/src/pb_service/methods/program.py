# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Downloading and running programs."""

from __future__ import annotations

import logging
from typing import Any

import anyio

from pb_core.jsonrpc import ErrorCode, JsonRpcError, decode_binary
from pb_service.errors import as_wire_error, hub_errors
from pb_service.hub import protocol as p
from pb_service.hub.connection import HubConnection, HubDisconnectedError
from pb_service.hub.events import Channel, StatusEvent
from pb_service.hub.transport import HubCommandError
from pb_service.methods import method, require
from pb_service.methods.hub import hub_of
from pb_service.session import Session

logger = logging.getLogger(__name__)

__all__ = ["program_download", "program_run", "program_start", "program_stop"]

_RUN_START_GRACE = 5.0
"""Seconds to wait for the hub to report the program as started."""


def _program_to_start(connection: HubConnection, slot: Any) -> int | None:
    """Decide which program a start command should name.

    Args:
        connection: The hub being started.
        slot: The slot the caller asked for, or ``None`` for "whichever the
            hub has selected".

    Returns:
        The program id to send, or ``None`` for the legacy no-argument form.
    """
    if slot is not None:
        return int(slot)
    info = connection.info
    if info is None or not p.start_takes_program_id(info.profile):
        # Nothing else to say on a hub whose command has no argument.
        return None
    status = connection.status
    if status is not None and status.selected_slot is not None:
        return status.selected_slot
    # A hub that holds one program at a time reports no selection, and that
    # one program is slot 0.
    return 0


def _download_slot(connection: HubConnection, slot: Any) -> int:
    """Work out where a download will land, and check it is where it was sent.

    The hub decides this, not the host. ``WRITE_USER_PROGRAM_META`` clears and
    fills whichever slot the hub has selected -- with its own buttons -- and
    there is no command in the protocol that selects one. So a caller naming a
    slot is stating an expectation rather than giving an instruction, and the
    only honest answer when it does not hold is to say so. The alternative is
    the worst kind of quiet: the program is stored in one slot, the start
    command names another, and what runs is whatever happened to be there
    already.

    Args:
        connection: The hub being written to.
        slot: The slot the caller expects, or ``None`` to accept the hub's.

    Returns:
        The slot the program will go into.

    Raises:
        JsonRpcError: If the hub has a different slot selected.
    """
    status = connection.status
    selected = None if status is None else status.selected_slot
    # A hub that holds one program at a time reports no selection, and that
    # one program is slot 0.
    destination = 0 if selected is None else selected
    if slot is None or int(slot) == destination:
        return destination

    info = connection.info
    slots = 1 if info is None else max(1, info.capabilities.num_slots)
    if slots == 1:
        message = f"this hub holds one program at a time, so there is no slot {slot}"
    else:
        message = (
            f"the hub has slot {destination} selected, and a program can only "
            f"be downloaded into the selected slot"
        )
    raise JsonRpcError(
        ErrorCode.PROGRAM_SLOT_NOT_SELECTED,
        message,
        # How many slots there are decides what the caller can usefully be
        # told to do about it, and only the hub knows.
        {"selected": destination, "asked": int(slot), "slots": slots},
    )


async def _download(
    connection: HubConnection,
    program: bytes,
) -> None:
    """Write a program into hub memory.

    The metadata is cleared first and only set to the real length once every
    chunk has landed, so an interrupted download can never leave something that
    looks runnable but is not.
    """
    info = connection.info
    if info is None:
        raise JsonRpcError(ErrorCode.HUB_NOT_CONNECTED, "hub is not ready")

    limit = info.capabilities.max_program_size
    if len(program) > limit:
        raise JsonRpcError(
            ErrorCode.PROGRAM_TOO_LARGE,
            f"program is {len(program)} bytes; this hub accepts at most {limit}",
            {"size": len(program), "limit": limit},
        )

    chunk_size = info.capabilities.max_user_ram_payload
    logger.info(
        "downloading %d bytes in %d chunk(s)",
        len(program),
        max(1, -(-len(program) // chunk_size)),
    )

    await connection.send_command(p.encode_write_user_program_meta(0))
    try:
        for offset in range(0, len(program), chunk_size):
            chunk = program[offset : offset + chunk_size]
            await connection.send_command(p.encode_write_user_ram(offset, chunk))
    except BaseException:
        # Leave nothing runnable behind, including when cancelled. Shielded
        # because this is exactly the case where the connection is going away.
        with anyio.CancelScope(shield=True), anyio.move_on_after(2):
            await connection.send_command(p.encode_write_user_program_meta(0))
        raise
    await connection.send_command(p.encode_write_user_program_meta(len(program)))


@method(
    "program.download",
    "Send a compiled program to a hub without starting it.",
    params={
        "id": "The hub, by name or id. Defaults to the current hub.",
        "mpy": "The compiled program, base64 encoded.",
        "slot": "The slot it is expected to land in, or null for the hub's own.",
    },
    returns="How many bytes were written, which slot they went into, and how "
    "many slots the hub has.",
)
async def program_download(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Download a program."""
    session.daemon.mark_activity()
    connection = hub_of(session, params)
    program = decode_binary(require(params, "mpy", str))
    slot = _download_slot(connection, params.get("slot"))
    with hub_errors("store the program"):
        async with connection.abort_on_disconnect():
            await _download(connection, program)
    info = connection.info
    return {
        "size": len(program),
        "slot": slot,
        "slots": 1 if info is None else max(1, info.capabilities.num_slots),
    }


async def _start_and_watch(
    connection: HubConnection, command: bytes, wait: bool, doing: str
) -> str:
    """Start a program, optionally staying until it stops.

    Args:
        connection: The hub to start it on.
        command: The encoded start command.
        wait: Whether to return only once the program has stopped.
        doing: What to say the hub would not do, if it refuses.

    Returns:
        ``"started"``, ``"stopped"`` or ``"disconnected"``.
    """
    # Subscribing before starting, so the program cannot finish between the
    # start command and our first look at the status.
    async with connection.subscribe(frozenset({Channel.STATUS})) as watcher:
        try:
            async with connection.abort_on_disconnect():
                await connection.send_command(command)
                if not wait:
                    return "started"
                return await _wait_for_stop(connection, watcher)
        except HubDisconnectedError:
            return "disconnected"
        except HubCommandError as exc:
            # A refusal is an answer, not a lost connection: say which command
            # the hub turned down.
            raise as_wire_error(exc, doing) from exc
        except anyio.get_cancelled_exc_class():
            # The caller gave up, so stop the program rather than leaving it
            # running. Shielded: we are already unwinding.
            with anyio.CancelScope(shield=True), anyio.move_on_after(2):
                await connection.send_command(p.encode_stop_user_program())
            raise


@method(
    "program.start",
    "Start a program that is already on the hub.",
    params={
        "id": "The hub, by name or id. Defaults to the current hub.",
        "slot": "Which program slot, or null for the one the hub has selected.",
        "wait": "Wait for the program to stop before replying. Default false.",
    },
    returns="Why it ended: started, stopped or disconnected.",
)
async def program_start(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Start a program."""
    session.daemon.mark_activity()
    connection = hub_of(session, params)
    info = connection.info
    if info is None:
        raise JsonRpcError(ErrorCode.HUB_NOT_CONNECTED, "hub is not ready")

    try:
        command = p.encode_start_user_program(
            _program_to_start(connection, params.get("slot")), profile=info.profile
        )
    except ValueError as exc:
        raise JsonRpcError(ErrorCode.HUB_UNSUPPORTED, str(exc)) from exc

    wait = bool(params.get("wait", False))
    return {"reason": await _start_and_watch(connection, command, wait, "start it")}


@method(
    "program.stop",
    "Stop the program running on a hub.",
    params={"id": "The hub, by name or id. Defaults to the current hub."},
    returns="An empty object.",
)
async def program_stop(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Stop the running program."""
    session.daemon.mark_activity()
    connection = hub_of(session, params)
    with hub_errors("stop the program"):
        await connection.send_command(p.encode_stop_user_program())
    return {}


@method(
    "program.run",
    "Download a program, start it, and optionally wait for it to finish.",
    params={
        "id": "The hub, by name or id. Defaults to the current hub.",
        "mpy": "The compiled program, base64 encoded.",
        "slot": "Which program slot to use.",
        "wait": "Wait for the program to stop before replying. Default true.",
    },
    returns="Why the run ended: stopped, cancelled or disconnected.",
)
async def program_run(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Download and run a program."""
    session.daemon.mark_activity()
    connection = hub_of(session, params)
    info = connection.info
    if info is None:
        raise JsonRpcError(ErrorCode.HUB_NOT_CONNECTED, "hub is not ready")

    program = decode_binary(require(params, "mpy", str))
    slot = params.get("slot")
    wait = params.get("wait", True)

    try:
        command = p.encode_start_user_program(
            _program_to_start(connection, slot), profile=info.profile
        )
    except ValueError as exc:
        raise JsonRpcError(ErrorCode.HUB_UNSUPPORTED, str(exc)) from exc

    try:
        async with connection.abort_on_disconnect():
            await _download(connection, program)
    except HubDisconnectedError:
        return {"reason": "disconnected", "size": len(program)}
    except HubCommandError as exc:
        raise as_wire_error(exc, "store that program") from exc

    reason = await _start_and_watch(connection, command, wait, "run it")
    return {"reason": reason, "size": len(program)}


async def _wait_for_stop(connection: HubConnection, watcher: Any) -> str:
    """Wait until the hub reports that no user program is running."""
    started = False
    with anyio.move_on_after(_RUN_START_GRACE) as grace:
        async for event in connection.events(watcher):
            if isinstance(event, StatusEvent) and event.report.program_running:
                started = True
                grace.deadline = float("inf")
                break

    if not started:
        # Never reported as running. Either it finished immediately or the hub
        # refused it; either way there is nothing left to wait for.
        logger.info("program never reported as running")
        return "stopped"

    async for event in connection.events(watcher):
        if isinstance(event, StatusEvent) and not event.report.program_running:
            return "stopped"
    return "disconnected"
