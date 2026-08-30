# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The ``pb`` command line.

Three rules shape everything here. Data goes to stdout and everything else --
progress, spinners, logs, prompts -- goes to stderr, so piping a command into
another program stays clean. Commands are thin: they parse arguments, call
``pb_client``, and render; no protocol logic lives in this package. And no
command catches its own failures, because a task group would have wrapped them
in an ``ExceptionGroup`` anyway -- :mod:`pb.errors` does that in one place, for
all of them. The only exceptions here are the few cases where a failure is a
perfectly normal answer, such as listing hubs when nothing is connected.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Generator, Iterable, Mapping
from pathlib import Path
from typing import Annotated

import cyclopts
from rich.console import Console
from rich.table import Table

import pb_client
from pb import __version__, errors
from pb.errors import EXIT_HUB_ERROR, EXIT_OK
from pb_core.jsonrpc import ErrorCode
from pb_core.logging import TRACE, setup_logging

logger = logging.getLogger(__name__)

__all__ = ["app", "main"]

SHOW_HUB_IDS: bool = False
"""Whether to print hub ids.

Off for now. A hub id is built from its Bluetooth address, which is not stable
-- and on macOS is a per-machine UUID standing in for one -- so printing it
invites users to write down something that will stop working. Ids remain in the
wire protocol and are still accepted as arguments; only the display is off.

Turn this on once a hub has a durable identifier worth showing. Annotated as
``bool`` rather than left to inference so that the code behind it keeps being
type-checked while it is switched off.
"""

_ID_PREFIXES = ("ble:", "fake:", "usb:", "rfcomm:")

HUB_WARNINGS: Mapping[str, str] = {
    "BATTERY_LOW_VOLTAGE_WARNING": "battery low",
    "BATTERY_LOW_VOLTAGE_SHUTDOWN": "battery empty, the hub is switching off",
    "BATTERY_HIGH_CURRENT": "drawing too much current",
    "BATTERY_HIGH_TEMP_WARNING": "battery hot",
    "BATTERY_HIGH_TEMP_SHUTDOWN": "battery too hot, the hub is switching off",
    "BLE_LOW_SIGNAL": "weak Bluetooth signal",
    "SHUTDOWN_REQUEST": "switching off",
    "SHUTDOWN": "switching off",
}
"""Hub status flags that are worth saying out loud, in the user's words.

Keyed on the wire name of the flag rather than on the bit, because the CLI
deliberately knows nothing about the hub protocol. Flags that describe a normal
state -- advertising, connected, a program running -- are missing on purpose:
they are either uninteresting or already a line of their own.
"""

_HANDLED_HERE = (pb_client.PbClientError, BaseExceptionGroup)
"""What to catch when a command deals with a failure itself.

Catching the error's own type is not enough: anything raised inside an
``async with pb_client.connect()`` block comes back wrapped in a group of one,
so the type to catch is not the type that was raised.
"""

_CONNECT_HINTS: Mapping[int, str] = {
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

app = cyclopts.App(
    name="pb",
    version=__version__,
    help=(
        "Talk to Pybricks hubs.\n\n"
        "A hub stays connected between commands, so you only need to run "
        "pb connect once."
    ),
)

service_app = cyclopts.App(
    name="service",
    help="Manage the background service that keeps hubs connected.",
)
app.command(service_app)


Verbose = Annotated[
    int,
    cyclopts.Parameter(
        name=["-v", "--verbose"],
        help="Increase log detail. Repeat for more (-v, -vv, -vvv).",
        show_default=False,
    ),
]


def _console(*, err: bool = False) -> Console:
    """Build a console for data (stdout) or for everything else (stderr)."""
    return Console(stderr=err)


@contextlib.contextmanager
def _null() -> Generator[None]:
    """Stand in for a spinner when output is not a terminal."""
    yield


def _configure_logging(verbose: int) -> None:
    """Map repeated ``-v`` flags onto log levels."""
    if verbose <= 0:
        level = logging.WARNING
    elif verbose == 1:
        level = logging.INFO
    elif verbose == 2:
        level = logging.DEBUG
    else:
        level = TRACE
    setup_logging(
        level=level,
        style="rich" if sys.stderr.isatty() else "plain",
        quiet_loggers=("bleak", "asyncio") if verbose < 3 else (),
    )


def _looks_like_id(value: str) -> bool:
    """Whether a string is a hub id rather than a hub name."""
    return value.startswith(_ID_PREFIXES)


def hub_warnings(status_names: Iterable[str]) -> list[str]:
    """Translate the hub's status flags into things worth telling the user.

    Args:
        status_names: Flag names as the hub reported them.

    Returns:
        The wording for each flag that says something actionable, in the order
        given and without repeats -- two flags can mean the same thing to a
        user, and saying it twice would look like a fault in the tool.
    """
    said: list[str] = []
    for name in status_names:
        warning = HUB_WARNINGS.get(name)
        if warning is not None and warning not in said:
            said.append(warning)
    return said


def _rows(out: Console, rows: list[tuple[str, str]]) -> None:
    """Render key/value rows, as a table on a terminal and tab-separated when piped."""
    if out.is_terminal:
        table = Table(show_header=False, box=None)
        for key, value in rows:
            table.add_row(key, value)
        out.print(table)
    else:
        for key, value in rows:
            out.print(f"{key}\t{value}", highlight=False, markup=False)


# ---------------------------------------------------------------------- hubs


@app.command(name="scan")
def scan_cmd(
    *, timeout: float = 10.0, name: str | None = None, verbose: Verbose = 0
) -> int:
    """Look for Pybricks hubs that are switched on and in range.

    Args:
        timeout: How long to scan, in seconds.
        name: Only show hubs with this name.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return errors.run(_scan, timeout, name)


async def _scan(seconds: float, name: str | None) -> int:
    out, err = _console(), _console(err=True)
    async with pb_client.connect() as daemon:
        spinner = (
            err.status(f"Scanning for {seconds:.0f}s...")
            if err.is_terminal
            else _null()
        )
        with spinner:
            devices = await daemon.scan(seconds=seconds, name=name)

    if not devices:
        err.print("[yellow]No hubs found.[/] Is the hub switched on and in range?")
        return EXIT_OK

    if out.is_terminal:
        table = Table()
        table.add_column("Name")
        if SHOW_HUB_IDS:
            table.add_column("Address", style="dim")
        table.add_column("Signal", justify="right")
        for device in devices:
            signal = f"{device.rssi} dBm" if device.rssi is not None else ""
            row = [device.name, device.id, signal]
            if not SHOW_HUB_IDS:
                del row[1]
            table.add_row(*row)
        out.print(table)
    else:
        for device in devices:
            line = f"{device.name}\t{device.id}" if SHOW_HUB_IDS else device.name
            out.print(line, highlight=False, markup=False)
    return EXIT_OK


@app.command(name="connect")
def connect_cmd(hub: str | None = None, /, *, verbose: Verbose = 0) -> int:
    """Connect to a hub and keep it connected.

    The connection outlives this command, so later commands reuse it rather
    than reconnecting.

    Args:
        hub: The hub's name, as shown by pb scan. Defaults to the first
            hub found.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return errors.run(_connect, hub, hints=_CONNECT_HINTS)


async def _connect(hub: str | None) -> int:
    err = _console(err=True)
    async with pb_client.connect() as daemon:
        spinner = err.status("Connecting...") if err.is_terminal else _null()
        with spinner:
            info = await daemon.connect_hub(
                hub_id=hub if hub and _looks_like_id(hub) else None,
                name=hub if hub and not _looks_like_id(hub) else None,
            )

    # The protocol version belongs to pb status: it explains why a command was
    # refused, but on a successful connection it is one more number to read
    # past.
    err.print(f"Connected to [bold]{info.name}[/] (firmware {info.firmware}).")
    return EXIT_OK


@app.command(name="disconnect")
def disconnect_cmd(hub: str | None = None, /, *, verbose: Verbose = 0) -> int:
    """Disconnect a hub.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return errors.run(_disconnect, hub)


async def _disconnect(hub: str | None) -> int:
    err = _console(err=True)
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


@app.command(name="list")
def list_cmd(*, verbose: Verbose = 0) -> int:
    """List the hubs that are currently connected.

    Args:
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return errors.run(_list)


async def _list() -> int:
    out, err = _console(), _console(err=True)
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

    # No state column: this lists what is connected, so it could only ever say
    # "connected". It comes back when the service tracks hubs it knows about
    # but is not holding.
    if out.is_terminal:
        table = Table()
        table.add_column("")
        table.add_column("Name")
        table.add_column("Firmware")
        table.add_column("Used by", justify="right")
        for info in hubs:
            table.add_row(
                "*" if info.id == default else "",
                info.name,
                info.firmware,
                str(info.subscribers),
            )
        out.print(table)
    else:
        for info in hubs:
            marker = "*" if info.id == default else "-"
            fields = [marker, info.name, info.id, info.firmware]
            if not SHOW_HUB_IDS:
                del fields[2]
            out.print("\t".join(fields), highlight=False, markup=False)
    return EXIT_OK


@app.command(name="status")
def status_cmd(
    hub: str | None = None, /, *, full: bool = False, verbose: Verbose = 0
) -> int:
    """Show what a hub is doing.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        full: Show every detail, including the ones that only mean something
            when you are debugging.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return errors.run(_status, hub, full)


async def _status(hub: str | None, full: bool) -> int:
    out = _console()
    async with pb_client.connect(autostart=False) as daemon:
        info = await daemon.hub_info(hub)

    rows = [("name", info.name)]
    if SHOW_HUB_IDS:
        rows.append(("id", info.id))
    rows.append(("state", info.state))
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
    warnings = hub_warnings(info.status_names)
    if warnings:
        rows.append(("warnings", ", ".join(warnings)))
    if full and info.status_names:
        rows.append(("flags", ", ".join(info.status_names)))

    _rows(out, rows)
    return EXIT_OK


@app.command(name="start")
def start_cmd(
    hub: str | None = None, /, *, slot: int | None = None, verbose: Verbose = 0
) -> int:
    """Start a program that is already on the hub.

    Nothing is sent to the hub, so this is the quick way to run the same
    program again, and the way to run one put there by another tool.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        slot: Which program slot. Defaults to the one the hub has selected.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return errors.run(_start, hub, slot)


async def _start(hub: str | None, slot: int | None) -> int:
    err = _console(err=True)
    async with pb_client.connect(autostart=False) as daemon:
        await daemon.start_program(hub_id=hub, slot=slot)
    err.print("Program started.")
    return EXIT_OK


@app.command(name="stop")
def stop_cmd(hub: str | None = None, /, *, verbose: Verbose = 0) -> int:
    """Stop the program running on a hub.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return errors.run(_stop, hub)


async def _stop(hub: str | None) -> int:
    err = _console(err=True)
    async with pb_client.connect(autostart=False) as daemon:
        await daemon.stop_program(hub)
    err.print("Stopped.")
    return EXIT_OK


@app.command(name="run")
def run_cmd(
    script: Path,
    hub: str | None = None,
    /,
    *,
    slot: int | None = None,
    wait: bool = True,
    verbose: Verbose = 0,
) -> int:
    """Compile a script, send it to a hub, and run it.

    Args:
        script: The Python file to run.
        hub: Which hub, by name. Defaults to the one in use.
        slot: Which program slot to use.
        wait: Wait for the program to finish before returning.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return errors.run(_run, script, hub, slot, wait)


async def _run(script: Path, hub: str | None, slot: int | None, wait: bool) -> int:
    err = _console(err=True)

    spinner = err.status("Compiling...") if err.is_terminal else _null()
    with spinner:
        program = await pb_client.compile_script(script)

    async with pb_client.connect(autostart=False) as daemon:
        spinner = (
            err.status(f"Downloading {len(program)} bytes...")
            if err.is_terminal
            else _null()
        )
        with spinner:
            reason = await daemon.run_program(program, hub_id=hub, slot=slot, wait=wait)

    if reason == "disconnected":
        err.print("[yellow]The hub disconnected while the program was running.[/]")
        return EXIT_HUB_ERROR
    if reason == "started":
        err.print("Program started.")
    else:
        err.print("Program finished.")
    return EXIT_OK


@app.command(name="download")
def download_cmd(
    script: Path, hub: str | None = None, /, *, verbose: Verbose = 0
) -> int:
    """Compile a script and send it to a hub without running it.

    Args:
        script: The Python file to send.
        hub: Which hub, by name. Defaults to the one in use.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return errors.run(_download, script, hub)


async def _download(script: Path, hub: str | None) -> int:
    err = _console(err=True)
    program = await pb_client.compile_script(script)

    async with pb_client.connect(autostart=False) as daemon:
        size = await daemon.download(program, hub_id=hub)

    err.print(f"Downloaded {size} bytes.")
    return EXIT_OK


# ------------------------------------------------------------------- service


@service_app.command(name="status")
def service_status(*, verbose: Verbose = 0, start: bool = False) -> int:
    """Show whether the background service is running.

    Args:
        verbose: Increase log detail.
        start: Start the service if it is not running, instead of reporting
            that it is not.
    """
    _configure_logging(verbose)
    return errors.run(_service_status, start)


async def _service_status(start: bool) -> int:
    out, err = _console(), _console(err=True)
    try:
        async with pb_client.connect(autostart=start) as daemon:
            info = await daemon.info()
    except _HANDLED_HERE as exc:
        # "It is not running" is the answer to the question, not a failure to
        # answer it, so it is reported here rather than as an error.
        if errors.find(exc, pb_client.DaemonUnavailable) is None:
            raise
        err.print("[yellow]Not running.[/]")
        err.print("It starts by itself when needed, or run [bold]pb service start[/].")
        return errors.EXIT_NOTHING_CONNECTED

    _rows(
        out,
        [
            ("status", "running"),
            ("version", info.version),
            ("protocol", str(info.protocol_version)),
            ("pid", str(info.pid)),
            ("uptime_s", f"{info.uptime_s:.1f}"),
            ("endpoint", info.endpoint),
            ("clients", str(info.clients)),
            ("log", info.log_file),
        ],
    )
    return EXIT_OK


@service_app.command(name="start")
def service_start(*, verbose: Verbose = 0) -> int:
    """Start the background service if it is not already running.

    Args:
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return errors.run(_service_start)


async def _service_start() -> int:
    err = _console(err=True)
    async with pb_client.connect(autostart=True) as daemon:
        info = await daemon.info()
    err.print(f"Running (pid {info.pid}).")
    return EXIT_OK


@service_app.command(name="stop")
def service_stop(*, verbose: Verbose = 0) -> int:
    """Stop the background service, disconnecting any hubs.

    Args:
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return errors.run(_service_stop)


async def _service_stop() -> int:
    err = _console(err=True)
    try:
        async with pb_client.connect(autostart=False) as daemon:
            await daemon.shutdown()
    except _HANDLED_HERE as exc:
        # Stopping something that is already stopped succeeded.
        if errors.find(exc, pb_client.DaemonUnavailable) is None:
            raise
        err.print("Not running.")
        return EXIT_OK
    err.print("Stopping.")
    return EXIT_OK


def main() -> int:
    """Run the CLI.

    Returns:
        The process exit code.
    """
    result = app()
    return result if isinstance(result, int) else EXIT_OK
