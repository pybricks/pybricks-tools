# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Hub-related RPC methods."""

from __future__ import annotations

import logging
from contextlib import aclosing
from typing import Any

import anyio

from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pbd.hub.connection import HubConnection, HubDisconnectedError
from pbd.hub.manager import HubAlreadyConnectedError, HubNotFoundError
from pbd.hub.transport import DeviceInfo, ScanFilter
from pbd.methods import method
from pbd.session import Session

logger = logging.getLogger(__name__)

__all__ = ["hub_connect", "hub_disconnect", "hub_info", "hub_list", "hub_scan"]

DEFAULT_SCAN_SECONDS = 10.0
CONNECT_SECONDS = 30.0


def _device_json(device: DeviceInfo) -> dict[str, Any]:
    return {
        "id": device.id,
        "name": device.name,
        "address": device.address,
        "rssi": device.rssi,
        "transport": device.transport,
    }


def _hub_json(connection: HubConnection) -> dict[str, Any]:
    info = connection.info
    status = connection.status
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
            "protocol": ".".join(str(part) for part in info.profile),
            "max_write_size": info.capabilities.max_write_size,
            "max_program_size": info.capabilities.max_program_size,
            "num_slots": info.capabilities.num_slots,
            "features": int(info.capabilities.flags),
            "feature_names": [
                flag.name for flag in info.capabilities.flags if flag.name
            ],
        }
    if status is not None:
        payload |= {
            "status_flags": int(status.flags),
            "status_names": [flag.name for flag in status.flags if flag.name],
            "program_running": status.program_running,
            "running_program_id": status.running_program_id,
            "selected_slot": status.selected_slot,
        }
    return payload


def _wrap_hub_errors(exc: Exception) -> JsonRpcError:
    """Translate a hub failure into the matching wire error."""
    if isinstance(exc, HubNotFoundError):
        return JsonRpcError(ErrorCode.HUB_NOT_FOUND, str(exc))
    if isinstance(exc, HubAlreadyConnectedError):
        return JsonRpcError(ErrorCode.HUB_ALREADY_CONNECTED, str(exc))
    if isinstance(exc, HubDisconnectedError):
        return JsonRpcError(ErrorCode.HUB_DISCONNECTED, str(exc))
    return JsonRpcError(ErrorCode.INTERNAL_ERROR, str(exc))


@method(
    "hub.scan",
    "Look for hubs that are advertising.",
    params={
        "timeout_s": "How long to look. Defaults to 10 seconds.",
        "name": "Only report hubs with this name.",
        "include_connected": "Also report hubs that are already connected.",
    },
    returns="The hubs seen, deduplicated by id.",
)
async def hub_scan(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Scan for hubs."""
    manager = session.daemon.hubs
    session.daemon.mark_activity()

    seconds = float(params.get("timeout_s") or DEFAULT_SCAN_SECONDS)
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
    manager = session.daemon.hubs
    session.daemon.mark_activity()

    wanted_id = params.get("id")
    wanted_name = params.get("name")

    if wanted_id and wanted_id in manager:
        return _hub_json(manager.get(str(wanted_id)))

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
        raise JsonRpcError(
            ErrorCode.HUB_NOT_FOUND,
            "no matching hub found; make sure it is switched on and in range",
        )

    try:
        with anyio.fail_after(CONNECT_SECONDS):
            await manager.connect(device, exclusive=bool(params.get("exclusive")))
    except TimeoutError:
        raise JsonRpcError(
            ErrorCode.HUB_NOT_CONNECTED, f"timed out connecting to {device.name}"
        ) from None
    except (HubNotFoundError, HubAlreadyConnectedError, HubDisconnectedError) as exc:
        raise _wrap_hub_errors(exc) from exc

    return _hub_json(manager.get(device.id))


@method(
    "hub.disconnect",
    "Disconnect a hub.",
    params={"id": "The hub to disconnect. Defaults to the current hub."},
    returns="An empty object.",
)
async def hub_disconnect(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Disconnect a hub."""
    manager = session.daemon.hubs
    session.daemon.mark_activity()
    hub_id = params.get("id")
    try:
        connection = manager.get(str(hub_id) if hub_id else None)
        await manager.disconnect(connection.id)
    except HubNotFoundError as exc:
        raise _wrap_hub_errors(exc) from exc
    return {}


@method(
    "hub.list",
    "List the hubs the daemon currently holds open.",
    returns="Every connected hub, and which one is the current default.",
)
async def hub_list(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """List connected hubs."""
    manager = session.daemon.hubs
    session.daemon.mark_activity()
    return {
        "hubs": [_hub_json(connection) for connection in manager.connected],
        "default": manager.default_hub_id,
    }


@method(
    "hub.info",
    "Report one hub's details and current status.",
    params={"id": "The hub to describe. Defaults to the current hub."},
    returns="The hub's identity, capabilities and status flags.",
)
async def hub_info(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Describe one hub."""
    manager = session.daemon.hubs
    session.daemon.mark_activity()
    hub_id = params.get("id")
    try:
        return _hub_json(manager.get(str(hub_id) if hub_id else None))
    except HubNotFoundError as exc:
        raise _wrap_hub_errors(exc) from exc


@method(
    "hub.use",
    "Choose the hub that later commands act on by default.",
    params={"id": "The hub to select, or null to clear the selection."},
    returns="The selected hub id.",
)
async def hub_use(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Set the default hub.

    Kept in the daemon rather than in a client config file so that it survives
    the command exiting and is shared between shells.
    """
    manager = session.daemon.hubs
    session.daemon.mark_activity()
    hub_id = params.get("id")
    if hub_id is None:
        manager.default_hub_id = None
        return {"default": None}
    try:
        connection = manager.resolve(str(hub_id))
    except HubNotFoundError as exc:
        raise _wrap_hub_errors(exc) from exc
    manager.default_hub_id = connection.id
    return {"default": connection.id}
