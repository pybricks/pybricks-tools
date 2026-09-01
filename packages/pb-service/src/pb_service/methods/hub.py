# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Hub-related RPC methods."""

from __future__ import annotations

import logging
from contextlib import aclosing
from typing import Any

import anyio

from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_core.protocol import HUB_FOUND
from pb_service.errors import HUB_FAILURES, as_wire_error
from pb_service.hub import protocol as p
from pb_service.hub.connection import HubConnection, HubDisconnectedError
from pb_service.hub.events import Channel, StatusEvent
from pb_service.hub.manager import (
    HubAlreadyConnectedError,
    HubManager,
    HubNotFoundError,
)
from pb_service.hub.transport import DeviceInfo, ScanFilter, looks_like_id
from pb_service.methods import method
from pb_service.session import Session

logger = logging.getLogger(__name__)

__all__ = [
    "hub_connect",
    "hub_disconnect",
    "hub_info",
    "hub_list",
    "hub_of",
    "hub_scan",
    "report_json",
    "status_json",
]

DEFAULT_SCAN_SECONDS = 10.0
CONNECT_SECONDS = 30.0

STOP_BUILTIN_SECONDS = 2.0
"""How long disconnecting waits for a built-in program to stop first."""


def _device_json(device: DeviceInfo) -> dict[str, Any]:
    return {
        "id": device.id,
        "name": device.name,
        "address": device.address,
        "rssi": device.rssi,
        "transport": device.transport,
        "type": device.hub_type,
    }


def _scan_seconds(params: dict[str, Any]) -> float | None:
    """Work out how long a scan should run.

    Absent and null mean different things, which is unusual enough to be worth
    the helper. A caller that says nothing gets the default; a caller that
    explicitly says null wants no limit, because it is a person choosing from
    the list and a deadline on a person only ever expires at the worst moment.
    Nothing runs away: the scan ends when the caller cancels the request or
    drops the connection.

    Args:
        params: The request parameters.

    Returns:
        Seconds to scan for, or ``None`` for no limit.
    """

    if "timeout_s" not in params:
        return DEFAULT_SCAN_SECONDS

    seconds = params["timeout_s"]
    return None if seconds is None else float(seconds)


def hub_of(session: Session, params: dict[str, Any]) -> HubConnection:
    """Find the hub a request is about.

    Args:
        session: The calling session.
        params: The request parameters, whose ``id`` names a hub or is absent
            for whichever hub is currently in use.

    Returns:
        The connection.

    Raises:
        JsonRpcError: If there is no such hub, or none in use.
    """
    hub_id = params.get("id")

    try:
        return session.service.hubs.get(str(hub_id) if hub_id else None)
    except HubNotFoundError as exc:
        raise JsonRpcError(ErrorCode.HUB_NOT_FOUND, str(exc)) from exc


def report_json(report: p.StatusReport) -> dict[str, Any]:
    """Describe one status report.

    Args:
        report: What the hub said.

    Returns:
        The status fields.
    """
    return {
        "status_flags": int(report.flags),
        "status_names": [flag.name for flag in report.flags if flag.name],
        "program_running": report.program_running,
        "running_program_id": report.running_program_id,
        "selected_slot": report.selected_slot,
    }


def status_json(connection: HubConnection) -> dict[str, Any]:
    """Describe what a hub is currently reporting about itself.

    Its own function because it is sent two ways: as part of a hub's
    description, and on its own as a ``hub.status`` notification whenever the
    flags change. Both carry the whole status rather than a description of what
    changed, because a status is a level rather than an event -- a client that
    misses one and catches the next is still right.

    Args:
        connection: The hub.

    Returns:
        The status fields, or an empty mapping if the hub has not reported yet.
    """
    report = connection.status
    return {} if report is None else report_json(report)


def _hub_json(connection: HubConnection) -> dict[str, Any]:
    info = connection.info
    payload: dict[str, Any] = {
        "id": connection.id,
        "state": str(connection.state),
        "subscribers": connection.subscriber_count,
    }

    if info is not None:
        payload |= {
            "name": info.name,
            "address": info.address,
            "transport": info.transport,
            "firmware": info.firmware,
            # What the hub is, in the words Pybricks Code uses. Absent rather
            # than guessed at when the PnP ID names something unfamiliar.
            "type": p.hub_type_name(info.pnp),
            "protocol": ".".join(str(part) for part in info.profile),
            "max_write_size": info.capabilities.max_write_size,
            "max_program_size": info.capabilities.max_program_size,
            "num_slots": info.capabilities.num_slots,
            "features": int(info.capabilities.flags),
            "feature_names": [
                flag.name for flag in info.capabilities.flags if flag.name
            ],
        }

    payload |= status_json(connection)
    return payload


@method(
    "hub.scan",
    "Look for hubs that are advertising. Each advertisement is also sent to "
    "the caller as a hub.found notification while the scan runs, so a client "
    "can show hubs as they appear instead of waiting for the whole scan.",
    params={
        "timeout_s": (
            "How long to look. Omit for 10 seconds; pass null to look until "
            "the caller stops asking, which is what an interactive chooser "
            "needs and is why null and absent mean different things here."
        ),
        "name": "Only report hubs with this name.",
        "include_connected": "Also report hubs that are already connected.",
    },
    returns="The hubs seen, deduplicated by id.",
)
async def hub_scan(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Scan for hubs."""
    manager = session.service.hubs
    session.service.mark_activity()

    seconds = _scan_seconds(params)
    name = params.get("name")
    exclude: frozenset[str] = frozenset()

    if not params.get("include_connected"):
        exclude = frozenset(c.id for c in manager.connected)

    scan_filter = ScanFilter(name=str(name) if name else None, exclude=exclude)
    found: dict[str, DeviceInfo] = {}

    # A cancel scope rather than a timeout argument, which also means that
    # whatever was seen before the deadline is still returned.
    with anyio.move_on_after(seconds):
        async with aclosing(manager.discover(scan_filter)) as stream:
            async for device in stream:
                found[device.id] = device

                # Every advertisement, not only the first from each hub: the
                # signal strength is what changes between them, and watching
                # it move is how someone finds out which hub is which. The
                # result still carries the whole list, so a caller that
                # ignores the notifications loses nothing.
                if session.peer is not None:
                    session.peer.notify(HUB_FOUND, _device_json(device))

    return {"devices": [_device_json(device) for device in found.values()]}


@method(
    "hub.connect",
    "Connect to a hub and keep it connected.",
    params={
        "id": "The hub's id, from hub.scan.",
        "name": "Connect to the first hub with this name instead.",
        "exclusive": "Fail if the hub is already connected.",
    },
    returns="The connected hub's details.",
)
async def hub_connect(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Connect to a hub, scanning for it first if needed."""
    manager = session.service.hubs
    session.service.mark_activity()

    wanted_id = params.get("id")
    wanted_name = params.get("name")

    if wanted_id and wanted_id in manager:
        return _hub_json(manager.get(str(wanted_id)))

    if not wanted_id and not wanted_name and manager.connected:
        # Connecting is what a user types to be sure they have a hub, so being
        # asked when there already is one is not an error and not a reason to
        # go looking for a second. It would look like one, too: a connected hub
        # has stopped advertising, so the scan would find nothing and the user
        # would be told to check that their hub is switched on.
        return _hub_json(manager.get(None))

    scan_filter = ScanFilter(name=str(wanted_name) if wanted_name else None)
    device: DeviceInfo | None = None

    with anyio.move_on_after(float(params.get("timeout_s") or DEFAULT_SCAN_SECONDS)):
        async with aclosing(manager.discover(scan_filter)) as stream:
            async for candidate in stream:
                if wanted_id and candidate.id != wanted_id:
                    continue

                device = candidate
                break

    if device is None:
        # Just the fact. What to do about it depends on what the caller was
        # trying to do, so the advice belongs to the client, not here.
        raise JsonRpcError(ErrorCode.HUB_NOT_FOUND, "no hub found")

    try:
        with anyio.fail_after(CONNECT_SECONDS):
            await manager.connect(device, exclusive=bool(params.get("exclusive")))
    except TimeoutError:
        raise JsonRpcError(
            ErrorCode.HUB_NOT_CONNECTED, f"timed out connecting to {device.name}"
        ) from None
    except (HubNotFoundError, HubAlreadyConnectedError, HubDisconnectedError) as exc:
        raise as_wire_error(exc) from exc

    return _hub_json(manager.get(device.id))


async def _stop_builtin_program(connection: HubConnection) -> None:
    """Stop a built-in program before dropping the connection.

    The REPL, port view and IMU calibration are conversations with whoever is
    attached: none of them means anything once nobody is. Worse, a hub only
    advertises when nothing is running on it, so disconnecting from one of
    these and leaving it going produces a hub that nothing can find afterwards
    -- recoverable only at the hub itself, by pressing its button or switching
    it off and on.

    A user's own program is left alone, because setting one going and walking
    away is a thing people deliberately do.

    Args:
        connection: The hub about to be disconnected.
    """
    status = connection.status
    if status is None or not status.program_running:
        return

    running = status.running_program_id
    if running is None or running < p.UserProgramId.REPL:
        return

    # Best effort and bounded: the connection is going either way, and a hub
    # that will not answer must not hold up the disconnect.
    try:
        # Subscribed before the command is sent, so the hub's answer cannot
        # arrive in the gap between the two. Waiting for that answer rather
        # than only sending the command is what puts "the program stopped"
        # ahead of "the hub disconnected" for everyone else attached: a
        # session watching both would otherwise have to guess whether the
        # interpreter it started is still running, and guessing wrong means
        # telling the user to go and rescue a hub that is perfectly fine.
        async with connection.subscribe(frozenset({Channel.STATUS})) as watcher:
            with anyio.move_on_after(STOP_BUILTIN_SECONDS) as bounded:
                await connection.send_command(p.encode_stop_user_program())

                async for event in connection.events(watcher):
                    if isinstance(event, StatusEvent) and not (
                        event.report.program_running
                    ):
                        break

        if bounded.cancel_called:
            logger.info("the hub did not confirm the built-in program had stopped")
        else:
            logger.info("stopped the built-in program before disconnecting")
    except HUB_FAILURES:
        logger.info("could not stop the built-in program", exc_info=True)


@method(
    "hub.disconnect",
    "Disconnect a hub.",
    params={"id": "The hub to disconnect, by name or id. Defaults to the current hub."},
    returns="An empty object.",
)
async def hub_disconnect(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Disconnect a hub."""
    manager = session.service.hubs
    session.service.mark_activity()
    hub_id = params.get("id")

    try:
        connection = manager.get(str(hub_id) if hub_id else None)
        await _stop_builtin_program(connection)
        await manager.disconnect(connection.id)
    except HubNotFoundError as exc:
        raise as_wire_error(exc) from exc

    return {}


@method(
    "hub.list",
    "List the hubs the service currently holds open.",
    returns="Every connected hub, which one commands act on, and the chosen name.",
)
async def hub_list(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """List connected hubs."""
    manager = session.service.hubs
    session.service.mark_activity()
    return {
        "hubs": [_hub_json(connection) for connection in manager.connected],
        "default": _acting_hub_id(manager),
        "chosen": manager.selection.name,
    }


@method(
    "hub.info",
    "Report one hub's details and current status.",
    params={"id": "The hub to describe, by name or id. Defaults to the current hub."},
    returns="The hub's identity, capabilities and status flags.",
)
async def hub_info(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Describe one hub."""
    manager = session.service.hubs
    session.service.mark_activity()
    hub_id = params.get("id")

    try:
        return _hub_json(manager.get(str(hub_id) if hub_id else None))
    except HubNotFoundError as exc:
        raise as_wire_error(exc) from exc


@method(
    "hub.use",
    "Choose the hub that later commands act on by default.",
    params={
        "id": "The hub to choose, by name or id, or null to forget the"
        " chosen one. A hub that is not connected may be chosen by name."
    },
    returns="The chosen hub's name, and whether it is connected.",
)
async def hub_use(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Choose the hub commands act on when none is named.

    Kept here rather than in a client config file so that it survives the
    command exiting and is shared between shells, and kept on disk so that it
    survives this service.

    What is remembered is a **name**, because a hub has nothing durable to
    remember it by -- see :mod:`pb_service.hub.selection`. A name also means a hub
    that is switched off can be chosen: pinning the hub a project belongs to
    is a reasonable thing to do before touching any hardware. An id is
    accepted for a hub that is connected, and stored as that hub's name; an
    id for a hub that is not connected is refused, since there is no name to
    turn it into.
    """
    manager = session.service.hubs
    session.service.mark_activity()
    wanted = params.get("id")
    if wanted is None:
        manager.selection.choose(None)
        return {"default": None, "connected": False}

    wanted = str(wanted)

    try:
        connection = manager.resolve(wanted)
    except HubNotFoundError as exc:
        if looks_like_id(wanted):
            raise as_wire_error(exc) from exc

        manager.selection.choose(wanted)
        return {"default": wanted, "connected": False}

    name = connection.info.name if connection.info else wanted
    manager.selection.choose(name)
    return {"default": name, "connected": True}


def _acting_hub_id(manager: HubManager) -> str | None:
    """Return the hub a command with no hub named would act on.

    Args:
        manager: The service's hubs.

    Returns:
        Its id, or None if nothing would answer. Not the same as the
        chosen hub: a hub can be chosen while it is switched off, and
        then the only hub connected is the one that answers.
    """

    try:
        return manager.get(None).id
    except HubNotFoundError:
        return None
