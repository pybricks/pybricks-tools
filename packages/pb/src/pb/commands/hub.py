# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Finding hubs, holding on to them, and asking what they are doing.

Each command is a thin adapter: it reads the arguments, hands the work to an
async body, and lets :mod:`pb.errors` present whatever goes wrong. No command
catches its own failures, because a task group would have wrapped them in an
``ExceptionGroup`` anyway -- the exceptions being the few places where failing
is a perfectly good answer to the question asked, such as listing hubs when
nothing is connected.
"""

from __future__ import annotations

import logging
from typing import Annotated

import cyclopts

import pb_client
from pb import errors, keys, picker, ui
from pb.app import HUB_COMMANDS, Verbose, app, configure_logging
from pb.errors import EXIT_OK
from pb_core.jsonrpc import ErrorCode

logger = logging.getLogger(__name__)

__all__ = ["connect_cmd", "disconnect_cmd", "list_cmd", "scan_cmd", "status_cmd"]

_ID_PREFIXES = ("ble:", "fake:", "usb:", "rfcomm:")

_HANDLED_HERE = (pb_client.PbClientError, BaseExceptionGroup)
"""What to catch when a command deals with a failure itself.

Catching the error's own type is not enough: anything raised inside an
``async with pb_client.connect()`` block comes back wrapped in a group of one,
so the type to catch is not the type that was raised.
"""

_CONNECT_HINTS = {
    ErrorCode.HUB_NOT_FOUND: (
        "Switch the hub on and press its button so that its light flashes. "
        "If it is new, is the Pybricks firmware installed?"
    ),
}
"""What to suggest when it is a hub to connect to that could not be found.

Everywhere else, a missing hub means one that was expected to be connected
already, and the suggestion is to run [bold]pb connect[/] -- which would be
silly advice for [bold]pb connect[/] itself.
"""


def _looks_like_id(value: str) -> bool:
    """Whether a string is a hub id rather than a hub name."""
    return value.startswith(_ID_PREFIXES)


@app.command(name="scan", group=HUB_COMMANDS, sort_key=4)
async def scan_cmd(
    *,
    timeout: float = 10.0,  # noqa: ASYNC109 - see below
    name: str | None = None,
    verbose: Verbose = 0,
) -> int:
    """Look for Pybricks hubs that are switched on and in range.

    Nothing is connected to, which is the point: this answers "is it on, and
    can I hear it?" without taking the hub away from whatever else might want
    it.

    The ``timeout`` parameter is what ASYNC109 exists to catch, and this is the
    exception it does not know about: it is the spelling of a command-line
    option, not a deadline threaded into an async API. It says how long the
    radio should listen, which is a real parameter of scanning, and reaches
    ``pb-client`` as ``seconds``. A caller wanting to bound its own wait still
    opens a cancel scope, as the house rules require.

    Args:
        timeout: How long to scan, in seconds.
        name: Only show hubs with this name.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_scan, timeout, name)


async def _scan(seconds: float, name: str | None) -> int:
    out, err = ui.console(), ui.console(err=True)
    seen: dict[str, pb_client.DeviceInfo] = {}
    caption = f"Scanning for {seconds:.0f}s..."

    async with (
        pb_client.connect() as daemon,
        daemon.scanning(seconds=seconds, name=name) as devices,
    ):
        if out.is_terminal:
            # Redrawn in place as advertisements arrive, so a hub appears the
            # moment it is heard and its signal moves as it is carried about.
            with ui.live(ui.scan_table((), caption), out) as display:
                async for device in devices:
                    seen[device.id] = device
                    display.update(ui.scan_table(seen.values(), caption), refresh=True)
                display.update(ui.scan_table(seen.values(), f"Found {len(seen)}."))
        else:
            # Piped: one line per hub, printed the moment it is first heard, so
            # that "pb scan | head -1" answers as soon as there is an answer.
            async for device in devices:
                if device.id not in seen:
                    line = (
                        f"{device.name}\t{device.id}"
                        if ui.SHOW_HUB_IDS
                        else device.name
                    )
                    ui.plain(out, line)
                seen[device.id] = device

    if not seen:
        err.print("[yellow]No hubs found.[/] Is the hub switched on and in range?")
    return EXIT_OK


@app.command(name="connect", group=HUB_COMMANDS, sort_key=0)
async def connect_cmd(
    *,
    hub: str | None = None,
    scan: Annotated[bool, cyclopts.Parameter(name=["-s", "--scan"])] = False,
    verbose: Verbose = 0,
) -> int:
    """Connect to a hub and keep it connected.

    The connection outlives this command, so later commands reuse it rather
    than reconnecting.

    Args:
        hub: The hub's name, as shown by pb scan. Defaults to the first
            hub found.
        scan: Watch for hubs and choose one from the list. There is no time
            limit, so the hub can be switched on whenever you like, and
            Ctrl-C gives up without connecting to anything.
        verbose: Increase log detail.
    """
    if scan and hub is not None:
        return ui.usage(
            "Choose a hub with --scan, or name one with --hub, but not both.",
            "Leave out [bold]--hub[/] to pick from the list.",
        )
    if scan and not keys.keys_available():
        # Better to say so than to sit waiting for a keystroke that cannot
        # arrive, which is what a piped or emulated terminal would do.
        return ui.usage(
            "Choosing a hub needs a terminal to read the arrow keys from.",
            "Name the hub with [bold]--hub[/] instead, or run "
            "[bold]pb scan[/] to see what is nearby.",
        )
    configure_logging(verbose)
    return await errors.run(_connect, hub, scan, hints=_CONNECT_HINTS)


async def _connect(hub: str | None, scan: bool) -> int:
    err = ui.console(err=True)
    async with pb_client.connect() as daemon:
        if scan:
            # No time limit: the hub may still be in the next room. The scan
            # ends when the block does, whether the user chose or gave up.
            async with daemon.scanning(seconds=None) as devices:
                device = await picker.choose_hub(err, devices, ui.chooser_table)
            if device is None:
                # Deciding not to connect is what was asked for, not a failure.
                err.print("No hub chosen.")
                return EXIT_OK
            with ui.spinner(err, "Connecting..."):
                info = await daemon.connect_hub(hub_id=device.id)
        else:
            with ui.spinner(err, "Connecting..."):
                info = await daemon.connect_hub(
                    hub_id=hub if hub and _looks_like_id(hub) else None,
                    name=hub if hub and not _looks_like_id(hub) else None,
                )

    # The protocol version belongs to pb status: it explains why a command was
    # refused, but on a successful connection it is one more number to read
    # past.
    described = f"firmware {info.firmware}"
    if info.hub_type:
        described = f"{info.hub_type}, {described}"
    err.print(f"Connected to [bold]{info.name}[/] ({described}).")
    return EXIT_OK


@app.command(name="disconnect", group=HUB_COMMANDS, sort_key=1)
async def disconnect_cmd(*, hub: str | None = None, verbose: Verbose = 0) -> int:
    """Disconnect a hub.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_disconnect, hub)


async def _disconnect(hub: str | None) -> int:
    err = ui.console(err=True)
    try:
        async with pb_client.connect(autostart=False) as daemon:
            await daemon.disconnect_hub(hub)
    except _HANDLED_HERE as exc:
        # Nothing connected means there is nothing to disconnect, which is the
        # outcome the user asked for rather than a failure.
        if errors.find(exc, pb_client.DaemonUnavailable) is None:
            raise
        err.print("No hub is connected.")
        return EXIT_OK
    err.print("Disconnected.")
    return EXIT_OK


@app.command(name="list", group=HUB_COMMANDS, sort_key=3)
async def list_cmd(*, verbose: Verbose = 0) -> int:
    """List the hubs that are currently connected.

    Args:
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_list)


async def _list() -> int:
    out, err = ui.console(), ui.console(err=True)
    try:
        async with pb_client.connect(autostart=False) as daemon:
            hubs, default = await daemon.list_hubs()
    except _HANDLED_HERE as exc:
        # An empty list is a real answer to "what is connected?".
        if errors.find(exc, pb_client.DaemonUnavailable) is None:
            raise
        err.print("No hubs are connected.")
        return EXIT_OK

    if not hubs:
        err.print("No hubs connected. Use [bold]pb connect[/].")
        return EXIT_OK

    if out.is_terminal:
        out.print(ui.connected_table(hubs, default))
    else:
        for info in hubs:
            marker = "*" if info.id == default else "-"
            fields = [marker, info.name, info.id, info.firmware]
            if not ui.SHOW_HUB_IDS:
                del fields[2]
            ui.plain(out, "\t".join(fields))
    return EXIT_OK


@app.command(name="status", group=HUB_COMMANDS, sort_key=2)
async def status_cmd(
    *, hub: str | None = None, full: bool = False, verbose: Verbose = 0
) -> int:
    """Show what a hub is doing.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        full: Show every detail, including the ones that only mean something
            when you are debugging.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_status, hub, full)


async def _status(hub: str | None, full: bool) -> int:
    out = ui.console()
    async with pb_client.connect(autostart=False) as daemon:
        info = await daemon.hub_info(hub)

    rows = [("name", info.name)]
    if ui.SHOW_HUB_IDS:
        rows.append(("id", info.id))
    rows.append(("state", info.state))
    if info.hub_type:
        rows.append(("type", info.hub_type))
    rows.append(("firmware", info.firmware))
    if full:
        rows.append(("protocol", info.protocol))
        # Firmware older than the multi-slot support reports none, and a hub
        # with zero program slots would be a stranger thing to read than no
        # line at all.
        if info.num_slots:
            rows.append(("slots", str(info.num_slots)))
    rows.append(("program", "running" if info.program_running else "stopped"))
    if full:
        rows.append(("used by", str(info.subscribers)))

    # The raw flag names are the firmware's vocabulary, not the user's. The few
    # that say something actionable are worth translating; the rest are only
    # interesting when something is being debugged, which is what --full is.
    warnings = ui.hub_warnings(info.status_names)
    if warnings:
        rows.append(("warnings", ", ".join(warnings)))
    if full and info.status_names:
        rows.append(("flags", ", ".join(info.status_names)))

    ui.rows(out, rows)
    return EXIT_OK
