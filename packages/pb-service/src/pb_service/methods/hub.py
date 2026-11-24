# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Hub-related RPC methods."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from contextlib import aclosing
from typing import Any

import anyio

from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_core.protocol import HUB_ADAPTERS, HUB_FOUND
from pb_service.errors import HUB_FAILURES, as_wire_error
from pb_service.hub import protocol as p
from pb_service.hub.connection import HubConnection, HubDisconnectedError
from pb_service.hub.events import Channel, StatusEvent
from pb_service.hub.manager import (
    HubAlreadyConnectedError,
    HubManager,
    HubNotFoundError,
)
from pb_service.hub.transport import (
    DeviceInfo,
    ScanFilter,
    UnavailableDevice,
    looks_like_id,
    preference_rank,
)
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


def _found_nothing(manager: HubManager, chosen: str | None = None) -> JsonRpcError:
    """Say as precisely as the transports allow why nothing turned up.

    A hub that is plugged in but whose port will not open is the opposite
    problem from no hub at all -- the user needs to fix their computer, not go
    and find their hub -- so it gets a code of its own rather than the wording
    being bent to cover both. So does a Bluetooth adapter that cannot be used,
    which hides every hub behind it.

    Args:
        manager: Whose transports did the looking.
        chosen: The chosen hub, if that is what was looked for. Named in the
            message because the user did not name it this time, and "no hub
            found" while another hub sits switched on beside them would read
            as the tool being broken.

    Returns:
        The error to raise. Factual either way: what to suggest depends on the
        platform and on what the user was trying to do, and the service knows
        neither.
    """
    unavailable = manager.transport.unavailable
    ports = [d for d in unavailable if d.kind == "port"]
    adapters = [d for d in unavailable if d.kind == "adapter"]

    # A refused port first: it is a hub actually there, while an adapter that
    # cannot be used only means hubs behind it could not be looked for.
    if ports:
        refused = ", ".join(f"{d.address} ({d.reason})" for d in ports)
        return JsonRpcError(
            ErrorCode.SERIAL_PORT_UNAVAILABLE,
            f"found a hub but could not open its port: {refused}",
        )

    if adapters:
        reasons = ", ".join(d.reason for d in adapters)
        # The reason as a word too, for the client to choose its advice by:
        # the sentence is the system's and differs between platforms.
        known = [d.availability for d in adapters if d.availability]
        return JsonRpcError(
            ErrorCode.BLUETOOTH_UNAVAILABLE,
            f"Bluetooth cannot be used: {reasons}",
            {"availability": known[0]} if known else None,
        )

    return _not_found(chosen)


def _not_found(chosen: str | None) -> JsonRpcError:
    """Say that no hub was found, naming the chosen one if that was looked for.

    Args:
        chosen: The chosen hub, if that is what was looked for.

    Returns:
        The error to raise.
    """
    if chosen is not None:
        # Marked as chosen in the data too, because the remedy may be to choose
        # again: a hub chosen last week can be one nobody is using any more.
        return JsonRpcError(
            ErrorCode.HUB_NOT_FOUND,
            f"the chosen hub {chosen!r} was not found",
            {"chosen": chosen},
        )

    return JsonRpcError(ErrorCode.HUB_NOT_FOUND, "no hub found")


PERMISSION_SECONDS = 120.0
"""How much longer a search may run while the system asks for permission.

On macOS the first use of Bluetooth Classic asks the user whether the service
may use it, by the name in :data:`~pb_service.backend.PERMISSION_NAME`, and
nothing can be found until they answer. Their time reading the
dialog is not counted against the search's own limit -- a ten-second scan would
otherwise run out while they read -- but not without end either: a Mac where
nobody is there to answer leaves the question open for good.
"""

FOLLOW_SECONDS = 0.1
"""How often a search looks at what its transports cannot use.

A poll, because transports keep that as state rather than announcing it, and
only for as long as one request is looking.
"""


def _adapters(manager: HubManager) -> tuple[UnavailableDevice, ...]:
    return tuple(d for d in manager.transport.unavailable if d.kind == "adapter")


async def _follow_adapters(
    manager: HubManager,
    session: Session,
    looking: anyio.CancelScope,
    waiting: Callable[[], bool],
    started: anyio.Event,
) -> None:
    """Keep the caller told about unusable adapters while a search runs.

    Only adapters, not refused ports. A port is a hub that was found, which
    the result or the error at the end says. An adapter is what decides
    whether anything can be found at all, and one of its states -- a
    permission dialog that is open -- is something the user has to act on
    now, while the search is still running.

    Args:
        manager: Whose transports are looking.
        session: Who to tell, as :data:`~pb_core.protocol.HUB_ADAPTERS`.
        looking: The search's deadline, held still while the system is asking
            the user for permission, up to :data:`PERMISSION_SECONDS`.
        waiting: Whether nothing has been found yet. Once something has, the
            permission no longer holds the search open.
        started: Set when this search's scan has the radio. A search queued
            behind another must not report that one's adapters, or have its
            deadline held by that one's permission dialog.
    """
    await started.wait()
    told: tuple[UnavailableDevice, ...] = ()
    held = 0.0
    then = anyio.current_time()

    while True:
        # Before the first look, so that the transports have started this
        # search: until then they still hold what the last one found, even
        # once it has the radio.
        await anyio.sleep(FOLLOW_SECONDS)
        now = anyio.current_time()
        adapters = _adapters(manager)

        if adapters != told:
            told = adapters

            if session.peer is not None:
                session.peer.notify(
                    HUB_ADAPTERS,
                    {
                        "unusable": [
                            {
                                "address": d.address,
                                "reason": d.reason,
                                "availability": d.availability,
                            }
                            for d in adapters
                        ]
                    },
                )

        asked = any(d.awaiting_permission for d in adapters)

        if asked and waiting() and held < PERMISSION_SECONDS:
            # The clock stands still while the user is being asked.
            looking.deadline += now - then
            held += now - then

        then = now


def _device_json(device: DeviceInfo) -> dict[str, Any]:
    return {
        "id": device.id,
        "name": device.name,
        "address": device.address,
        "rssi": device.rssi,
        "transport": device.transport,
        "type": device.hub_type,
    }


def _present(devices: Iterable[DeviceInfo]) -> list[DeviceInfo]:
    """Leave out hubs that are only known of, if any were actually heard.

    RFCOMM lists the paired hubs, switched on or not, so a hub on it is no
    evidence of anything when another transport heard one: an EV3 paired last
    term and left in a drawer is neither a second hub to ask about nor a better
    choice than the hub advertising on the desk. Heard on its own, though, it is
    all there is to go on.

    Args:
        devices: What a search found.

    Returns:
        The hubs to choose between.
    """
    devices = list(devices)
    heard = [device for device in devices if device.transport != "rfcomm"]
    return heard or devices


def _several(devices: Iterable[DeviceInfo]) -> bool:
    """Whether these are certainly more than one hub.

    Two ids on the same transport are. Across transports the same hub turns up
    under a different id each way, and taking a hub on a cable and over
    Bluetooth for two hubs would ask the user to choose between one -- but it
    has the same name both ways, so two names are two hubs wherever they were
    heard.

    Args:
        devices: What a search found, at most one entry per id.

    Returns:
        True if some transport reported two different hubs, or two names were
        heard.
    """
    devices = list(devices)
    transports = [device.transport for device in devices]

    if len(transports) != len(set(transports)):
        return True

    return len({device.name for device in devices}) > 1


PREFERENCE_SECONDS = 0.5
"""How long a hub found on one transport waits for a better route to appear.

A hub can be reachable more than one way, and which way it is reached decides
how quick everything afterwards will be -- so taking whichever route answered
first would make that a race rather than a decision. It is a short wait, paid
only when a hub is asked for by id and the route that answered is not already
the preferred one. Without an id the longer AMBIGUITY_SECONDS is waited instead,
USB or not, since then the question is also whether there is another hub.
"""

AMBIGUITY_SECONDS = 1.0
"""How long a hub found without an id waits for another that would match too.

Longer than the wait for a better route, because a second hub has to be heard
before it can be refused, and hubs advertise only so often: measured on
Windows, gaps between a hub's advertisements of up to 0.8 s with current
firmware, and of a second with some City hub firmware. A hub quieter
than this is still missed, and connected to without asking, as before.
"""

PAIRED_SECONDS = 3.0
"""How long a paired hub found without an id waits for a hub actually heard.

RFCOMM lists the paired hubs at once, switched on or not, so one of them is no
sign that anything is there. Starting AMBIGUITY_SECONDS from it would end the
search before Bluetooth had heard the hubs that are -- Windows takes a moment
to start watching, and a hub is heard only once its scan response is in. This
is the wait for that instead, and the first hub heard on another transport
starts AMBIGUITY_SECONDS afresh. It is what connecting an EV3 costs when no
other hub is about.
"""


def _transport(params: dict[str, Any]) -> str | None:
    """Read which transport a request asked for.

    Args:
        params: The request parameters.

    Returns:
        The transport's scheme, or ``None`` to use every transport the service
        has. Not validated against the transports that exist: a request for one
        this service was not built with finds nothing, which is a search with no
        result rather than a malformed request, and reads to the user as the hub
        not being there.
    """
    transport = params.get("transport")

    return str(transport).lower() if transport else None


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


def _hand_back(
    connection: HubConnection, params: dict[str, Any], *, by_id: bool = False
) -> dict[str, Any] | None:
    """Hand back a hub already connected, if it is what ``hub.connect`` asked for.

    Args:
        connection: The connected hub that would do.
        params: The request.
        by_id: Whether it was asked for by id. An id names the route as well
            as the hub, so another transport asked for alongside it is not a
            route to search: no hub on it can have that id.

    Returns:
        The hub, as ``hub.connect`` describes it; or ``None`` if the request
        asked for another route to it, which makes it a hub to look for that
        way -- which route is in use decides how quick everything after it is.

    Raises:
        JsonRpcError: With ``HUB_ALREADY_CONNECTED`` if the request was
            ``exclusive``, which is a request for a hub nothing else holds.
    """
    wanted = _transport(params)
    using = connection.info.transport if connection.info is not None else None

    if wanted is not None and wanted != using and not by_id:
        return None

    if params.get("exclusive"):
        # Named as the user knows it: ids are not shown to anyone.
        name = connection.info.name if connection.info is not None else "the hub"
        raise as_wire_error(HubAlreadyConnectedError(f"{name} is already connected"))

    return _connected_json(connection, already=True)


def _connected_json(connection: HubConnection, *, already: bool) -> dict[str, Any]:
    """Describe the hub ``hub.connect`` hands back.

    Args:
        connection: The hub.
        already: Whether it was connected before this call, so that a client
            can say so rather than claim to have just connected it.

    Returns:
        The hub, as ``hub.info`` describes it, and whether it already was.
    """
    return _hub_json(connection) | {"already_connected": already}


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
    "can show hubs as they appear instead of waiting for the whole scan. A "
    "Bluetooth adapter that cannot be used is reported as hub.adapters when "
    "that changes; while the system asks the user for permission, the time "
    "limit stands still.",
    params={
        "timeout_s": (
            "How long to look. Omit for 10 seconds; pass null to look until "
            "the caller stops asking, which is what an interactive chooser "
            "needs and is why null and absent mean different things here."
        ),
        "name": "Only report hubs with this name.",
        "include_connected": "Also report hubs that are already connected.",
        "transport": (
            "Only look on this transport, such as usb or ble. Omit to look on "
            "every one the service has, which is the usual thing to do."
        ),
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

    scan_filter = ScanFilter(
        name=str(name) if name else None,
        exclude=exclude,
        transport=_transport(params),
    )
    found: dict[str, DeviceInfo] = {}

    # A cancel scope rather than a timeout argument, which also means that
    # whatever was seen before the deadline is still returned.
    started = anyio.Event()

    async with anyio.create_task_group() as tg:
        with anyio.move_on_after(seconds) as looking:
            tg.start_soon(
                _follow_adapters,
                manager,
                session,
                looking,
                lambda: not found,
                started,
            )

            async with aclosing(
                manager.discover(scan_filter, started=started)
            ) as stream:
                async for device in stream:
                    found[device.id] = device

                    # Every advertisement, not only the first from each hub:
                    # the signal strength is what changes between them, and
                    # watching it move is how someone finds out which hub is
                    # which. The result still carries the whole list, so a
                    # caller that ignores the notifications loses nothing.
                    if session.peer is not None:
                        session.peer.notify(HUB_FOUND, _device_json(device))

        tg.cancel_scope.cancel()

    # A scan that finds nothing is an ordinary answer and not an error, so it
    # returns an empty list -- unless a hub was there and would not open, or
    # Bluetooth could not be used, which scanning again will not fix and which
    # the user can do something about. Only if it scanned at all: one whose
    # time ran out waiting behind another would be reporting that one's.
    if not found and started.is_set() and manager.transport.unavailable:
        raise _found_nothing(manager)

    return {"devices": [_device_json(device) for device in found.values()]}


@method(
    "hub.connect",
    "Connect to a hub and keep it connected. With no id or name, the hub "
    "chosen with hub.use if there is one; otherwise one already connected; "
    "otherwise the only hub found, or an ambiguity error listing them if there "
    "are several. Several hubs with the name asked for, or chosen, are an "
    "ambiguity error too. While looking for it, an unusable Bluetooth adapter is "
    "reported as hub.adapters, as by hub.scan.",
    params={
        "id": "The hub's id, from hub.scan.",
        "name": (
            "Connect to the hub with this name instead. One already connected "
            "is handed back; several found are an ambiguity error."
        ),
        "exclusive": "Fail if the hub is already connected.",
        "transport": (
            "Reach the hub this way, such as usb or ble. Omit to use whichever "
            "is available, preferring USB where a hub is reachable both ways."
        ),
    },
    returns=(
        "The connected hub's details, as hub.info gives them, with "
        "already_connected saying whether it was connected before this call."
    ),
)
async def hub_connect(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Connect to a hub, scanning for it first if needed."""
    manager = session.service.hubs
    session.service.mark_activity()

    wanted_id = params.get("id")
    wanted_name = params.get("name")
    chosen: str | None = None

    if (
        wanted_id
        and wanted_id in manager
        and (handed := _hand_back(manager.get(str(wanted_id)), params, by_id=True))
    ):
        return handed

    # Nothing named means the same here as for every other command: the chosen
    # hub, then whatever is connected. Connecting is not the exception it once
    # was. A hub can be chosen before it is switched on precisely so that the
    # next connect finds it, and connecting to some other hub instead leaves
    # the user with one hub chosen and a different one connected.
    unnamed = not wanted_id and not wanted_name

    if unnamed:
        chosen = manager.selection.name
        wanted_name = chosen

    # A name, typed or chosen, is a hub already connected before it is anything
    # to scan for: a connected hub has stopped advertising, so the scan would
    # pass it by and connect another hub of the same name instead.
    if wanted_name and not wanted_id:
        try:
            connection = manager.resolve(str(wanted_name))
        except HubNotFoundError:
            connection = None

        if connection is not None and (handed := _hand_back(connection, params)):
            return handed
    elif unnamed and manager.connected:
        # Connecting is what a user types to be sure they have a hub, so being
        # asked when there already is one is not an error and not a reason to
        # go looking for a second. It would look like one, too: a connected hub
        # has stopped advertising, so the scan would find nothing and the user
        # would be told to check that their hub is switched on.
        #
        # The one a command would act on, unless another route was asked for
        # and some other connected hub is on it.
        wanted_transport = _transport(params)
        connection = manager.get(None)

        def on(candidate: HubConnection) -> bool:
            return (
                candidate.info is not None
                and candidate.info.transport == wanted_transport
            )

        if wanted_transport is not None and not on(connection):
            connection = next(filter(on, manager.connected), connection)

        if handed := _hand_back(connection, params):
            return handed

    # Anything short of an id has to come down to one hub, or it is a guess.
    # With nothing named that is plainly so; with a name it is too, because
    # every hub flashed without one is called Pybricks Hub, so in a classroom
    # a name -- typed, or remembered by pb use -- can match half the room.
    # Hubs are told apart within a transport only, because one hub on a cable
    # and over Bluetooth has two ids and nothing here can tell that it is the
    # same hub.
    refuse_several = not wanted_id
    found: dict[str, DeviceInfo] = {}
    # Whether a hub has been heard on a transport that hears them, rather than
    # only listed as paired.
    heard = False
    # When the search would have ended had a paired hub not capped it. Kept
    # rather than read at the start, because waiting on a permission dialog
    # moves the end later until something is found.
    uncapped: float | None = None

    scan_filter = ScanFilter(
        name=str(wanted_name) if wanted_name else None,
        transport=_transport(params),
    )
    device: DeviceInfo | None = None

    # The scope is entered outside the aclosing block, and the loop below sets
    # its deadline rather than opening a scope of its own. A cancel scope that
    # bounds iteration of a generator has to sit outside the block that closes
    # it: put it inside and cancelling it unwinds into the generator's own
    # scopes, which anyio reports as exiting a cancel scope that is not the
    # current one -- measured, as a service that died whenever USB was in play.
    started = anyio.Event()

    async with anyio.create_task_group() as tg:
        with anyio.move_on_after(
            float(params.get("timeout_s") or DEFAULT_SCAN_SECONDS)
        ) as looking:
            tg.start_soon(
                _follow_adapters,
                manager,
                session,
                looking,
                lambda: device is None,
                started,
            )

            async with aclosing(
                manager.discover(scan_filter, started=started)
            ) as stream:
                async for candidate in stream:
                    if wanted_id and candidate.id != wanted_id:
                        continue

                    found[candidate.id] = candidate

                    if device is None or preference_rank(
                        candidate.transport
                    ) < preference_rank(device.transport):
                        device = candidate

                    if preference_rank(device.transport) == 0 and not refuse_several:
                        # Nothing can beat it, so do not wait out the grace.
                        # Short of an id, though, another hub may be on the
                        # next port, and only waiting can tell.
                        break

                    # Something matched, so stop waiting for a hub and start
                    # waiting for a better route to the one already found, and,
                    # short of an id, for any other hub that matches as well.
                    now = anyio.current_time()

                    if not refuse_several:
                        looking.deadline = min(
                            looking.deadline, now + PREFERENCE_SECONDS
                        )
                    elif candidate.transport == "rfcomm":
                        # Paired, which is not the same as there: give the
                        # transports that hear hubs time to hear one.
                        if not heard:
                            if uncapped is None:
                                uncapped = looking.deadline

                            looking.deadline = min(
                                looking.deadline, now + PAIRED_SECONDS
                            )
                    elif not heard:
                        # The first hub actually heard starts the wait afresh,
                        # whatever a paired one had set it to.
                        heard = True
                        looking.deadline = min(
                            looking.deadline if uncapped is None else uncapped,
                            now + AMBIGUITY_SECONDS,
                        )

        tg.cancel_scope.cancel()

    if device is None:
        # As for a scan: what the transports say is another search's unless
        # this one got the radio.
        if not started.is_set():
            raise _not_found(chosen)

        raise _found_nothing(manager, chosen)

    present = _present(found.values())

    if refuse_several and _several(present):
        # Bounded by AMBIGUITY_SECONDS after the first hub heard on a transport
        # that hears them, which is what a lone hub costs extra to connect to,
        # or by PAIRED_SECONDS when only paired hubs were found. A second hub
        # that has not advertised within it is missed, which is no worse than
        # before this check, and so is a second USB hub whose port takes longer
        # than that to answer the first time it is opened.
        devices = [_device_json(d) for d in present]

        if wanted_name:
            # Named in the data as well, because the remedy is different: no
            # choosing by name can tell these apart, only renaming one can.
            raise JsonRpcError(
                ErrorCode.HUB_AMBIGUOUS,
                f"more than one hub named {wanted_name!r} found",
                {"devices": devices, "name": str(wanted_name)},
            )

        names = ", ".join(sorted({d.name for d in present}))
        raise JsonRpcError(
            ErrorCode.HUB_AMBIGUOUS,
            f"more than one hub found: {names}",
            {"devices": devices},
        )

    # The best route among the hubs heard, which is the hub found when there was
    # only the one, reached the quickest way.
    device = min(present, key=lambda found: preference_rank(found.transport))

    try:
        with anyio.fail_after(CONNECT_SECONDS):
            await manager.connect(device, exclusive=bool(params.get("exclusive")))
    except TimeoutError:
        raise JsonRpcError(
            ErrorCode.HUB_NOT_CONNECTED, f"timed out connecting to {device.name}"
        ) from None
    except ConnectionError as exc:
        # How every transport reports a hub it cannot reach. Routine on
        # RFCOMM, where the listing is the paired set and so names hubs that
        # are switched off, and reachable on USB too by unplugging between
        # the scan and the connect.
        logger.info("could not connect to %s: %s", device.name, exc)
        raise JsonRpcError(
            ErrorCode.HUB_NOT_CONNECTED, f"could not connect to {device.name}"
        ) from exc
    except (HubNotFoundError, HubAlreadyConnectedError, HubDisconnectedError) as exc:
        raise as_wire_error(exc) from exc

    return _connected_json(manager.get(device.id), already=False)


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
