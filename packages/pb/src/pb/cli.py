# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The ``pb`` command line.

Two rules shape everything here. Data goes to stdout and everything else --
progress, spinners, logs, prompts -- goes to stderr, so piping a command into
another program stays clean. And commands are thin: they parse arguments, call
``pb_client``, and render. No protocol logic lives in this package.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import anyio
import cyclopts
from rich.console import Console
from rich.table import Table

import pb_client
from pb import __version__
from pb_core.logging import TRACE, setup_logging

logger = logging.getLogger(__name__)

__all__ = ["app", "main"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_HUB_ERROR = 3
EXIT_DAEMON_UNAVAILABLE = 4

_ID_PREFIXES = ("ble:", "fake:", "usb:", "rfcomm:")

app = cyclopts.App(
    name="pb",
    version=__version__,
    help=(
        "Talk to Pybricks hubs.\n\n"
        "A background daemon holds hub connections open, so a hub stays "
        "connected between commands."
    ),
)

daemon_app = cyclopts.App(name="daemon", help="Manage the background daemon.")
app.command(daemon_app)


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
def _null() -> Iterator[None]:
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


def _report_hub_error(err: Console, exc: pb_client.JsonRpcError) -> int:
    err.print(f"[red]{exc.message}[/]")
    return EXIT_HUB_ERROR


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
    return anyio.run(_scan, timeout, name, backend="asyncio")


async def _scan(seconds: float, name: str | None) -> int:
    out, err = _console(), _console(err=True)
    try:
        async with pb_client.connect() as daemon:
            spinner = (
                err.status(f"Scanning for {seconds:.0f}s...")
                if err.is_terminal
                else _null()
            )
            with spinner:
                devices = await daemon.scan(seconds=seconds, name=name)
    except pb_client.DaemonStartError as exc:
        err.print(f"[red]{exc}[/]")
        return EXIT_ERROR

    if not devices:
        err.print("[yellow]No hubs found.[/] Is the hub switched on and in range?")
        return EXIT_OK

    if out.is_terminal:
        table = Table()
        table.add_column("Name")
        table.add_column("Id", style="dim")
        table.add_column("Signal", justify="right")
        for device in devices:
            signal = f"{device.rssi} dBm" if device.rssi is not None else ""
            table.add_row(device.name, device.id, signal)
        out.print(table)
    else:
        for device in devices:
            out.print(f"{device.name}\t{device.id}", highlight=False, markup=False)
    return EXIT_OK


@app.command(name="connect")
def connect_cmd(hub: str | None = None, *, verbose: Verbose = 0) -> int:
    """Connect to a hub and keep it connected.

    The connection outlives this command, so later commands reuse it rather
    than reconnecting.

    Args:
        hub: The hub's name or id. Defaults to the first one found.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return anyio.run(_connect, hub, backend="asyncio")


async def _connect(hub: str | None) -> int:
    err = _console(err=True)
    try:
        async with pb_client.connect() as daemon:
            spinner = err.status("Connecting...") if err.is_terminal else _null()
            with spinner:
                info = await daemon.connect_hub(
                    hub_id=hub if hub and _looks_like_id(hub) else None,
                    name=hub if hub and not _looks_like_id(hub) else None,
                )
    except pb_client.JsonRpcError as exc:
        return _report_hub_error(err, exc)
    except pb_client.DaemonStartError as exc:
        err.print(f"[red]{exc}[/]")
        return EXIT_ERROR

    err.print(
        f"Connected to [bold]{info.name}[/] "
        f"(firmware {info.firmware}, protocol {info.protocol})."
    )
    return EXIT_OK


@app.command(name="disconnect")
def disconnect_cmd(hub: str | None = None, *, verbose: Verbose = 0) -> int:
    """Disconnect a hub.

    Args:
        hub: Which hub. Defaults to the current one.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return anyio.run(_disconnect, hub, backend="asyncio")


async def _disconnect(hub: str | None) -> int:
    err = _console(err=True)
    try:
        async with pb_client.connect(autostart=False) as daemon:
            await daemon.disconnect_hub(hub)
    except pb_client.DaemonUnavailable:
        err.print("No daemon is running, so no hub is connected.")
        return EXIT_OK
    except pb_client.JsonRpcError as exc:
        return _report_hub_error(err, exc)
    err.print("Disconnected.")
    return EXIT_OK


@app.command(name="list")
def list_cmd(*, verbose: Verbose = 0) -> int:
    """List the hubs the daemon is holding open.

    Args:
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return anyio.run(_list, backend="asyncio")


async def _list() -> int:
    out, err = _console(), _console(err=True)
    try:
        async with pb_client.connect(autostart=False) as daemon:
            hubs, default = await daemon.list_hubs()
    except pb_client.DaemonUnavailable:
        err.print("No daemon is running, so no hubs are connected.")
        return EXIT_OK

    if not hubs:
        err.print("No hubs connected. Use [bold]pb connect[/].")
        return EXIT_OK

    if out.is_terminal:
        table = Table()
        table.add_column("")
        table.add_column("Name")
        table.add_column("State")
        table.add_column("Firmware")
        table.add_column("Clients", justify="right")
        for info in hubs:
            table.add_row(
                "*" if info.id == default else "",
                info.name,
                info.state,
                info.firmware,
                str(info.subscribers),
            )
        out.print(table)
    else:
        for info in hubs:
            marker = "*" if info.id == default else "-"
            out.print(
                f"{marker}\t{info.name}\t{info.id}\t{info.state}",
                highlight=False,
                markup=False,
            )
    return EXIT_OK


@app.command(name="status")
def status_cmd(hub: str | None = None, *, verbose: Verbose = 0) -> int:
    """Show a hub's details and current status.

    Args:
        hub: Which hub. Defaults to the current one.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return anyio.run(_status, hub, backend="asyncio")


async def _status(hub: str | None) -> int:
    out, err = _console(), _console(err=True)
    try:
        async with pb_client.connect(autostart=False) as daemon:
            info = await daemon.hub_info(hub)
    except pb_client.DaemonUnavailable:
        err.print("No daemon is running.")
        return EXIT_DAEMON_UNAVAILABLE
    except pb_client.JsonRpcError as exc:
        return _report_hub_error(err, exc)

    rows = [
        ("name", info.name),
        ("id", info.id),
        ("state", info.state),
        ("firmware", info.firmware),
        ("protocol", info.protocol),
        ("slots", str(info.num_slots)),
        ("program", "running" if info.program_running else "stopped"),
        ("clients", str(info.subscribers)),
    ]
    if info.status_names:
        rows.append(("flags", ", ".join(info.status_names)))
    _rows(out, rows)
    return EXIT_OK


@app.command(name="stop")
def stop_cmd(hub: str | None = None, *, verbose: Verbose = 0) -> int:
    """Stop the program running on a hub.

    Args:
        hub: Which hub. Defaults to the current one.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return anyio.run(_stop, hub, backend="asyncio")


async def _stop(hub: str | None) -> int:
    err = _console(err=True)
    try:
        async with pb_client.connect(autostart=False) as daemon:
            await daemon.stop_program(hub)
    except pb_client.DaemonUnavailable:
        err.print("No daemon is running.")
        return EXIT_DAEMON_UNAVAILABLE
    except pb_client.JsonRpcError as exc:
        return _report_hub_error(err, exc)
    err.print("Stopped.")
    return EXIT_OK


@app.command(name="run")
def run_cmd(
    script: Path,
    hub: str | None = None,
    *,
    slot: int | None = None,
    wait: bool = True,
    verbose: Verbose = 0,
) -> int:
    """Compile a script, send it to a hub, and run it.

    Args:
        script: The Python file to run.
        hub: Which hub. Defaults to the current one.
        slot: Which program slot to use.
        wait: Wait for the program to finish before returning.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return anyio.run(_run, script, hub, slot, wait, backend="asyncio")


async def _run(script: Path, hub: str | None, slot: int | None, wait: bool) -> int:
    err = _console(err=True)

    try:
        spinner = err.status("Compiling...") if err.is_terminal else _null()
        with spinner:
            program = await pb_client.compile_script(script)
    except pb_client.CompileError as exc:
        err.print(f"[red]{exc}[/]")
        if exc.output:
            err.print(exc.output.rstrip(), highlight=False, markup=False)
        return EXIT_ERROR

    try:
        async with pb_client.connect(autostart=False) as daemon:
            spinner = (
                err.status(f"Downloading {len(program)} bytes...")
                if err.is_terminal
                else _null()
            )
            with spinner:
                reason = await daemon.run_program(
                    program, hub_id=hub, slot=slot, wait=wait
                )
    except pb_client.DaemonUnavailable:
        err.print("No daemon is running. Connect a hub first with [bold]pb connect[/].")
        return EXIT_DAEMON_UNAVAILABLE
    except pb_client.JsonRpcError as exc:
        return _report_hub_error(err, exc)

    if reason == "disconnected":
        err.print("[yellow]The hub disconnected while the program was running.[/]")
        return EXIT_HUB_ERROR
    if reason == "started":
        err.print("Program started.")
    else:
        err.print("Program finished.")
    return EXIT_OK


@app.command(name="download")
def download_cmd(script: Path, hub: str | None = None, *, verbose: Verbose = 0) -> int:
    """Compile a script and send it to a hub without running it.

    Args:
        script: The Python file to send.
        hub: Which hub. Defaults to the current one.
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return anyio.run(_download, script, hub, backend="asyncio")


async def _download(script: Path, hub: str | None) -> int:
    err = _console(err=True)
    try:
        program = await pb_client.compile_script(script)
    except pb_client.CompileError as exc:
        err.print(f"[red]{exc}[/]")
        if exc.output:
            err.print(exc.output.rstrip(), highlight=False, markup=False)
        return EXIT_ERROR

    try:
        async with pb_client.connect(autostart=False) as daemon:
            size = await daemon.download(program, hub_id=hub)
    except pb_client.DaemonUnavailable:
        err.print("No daemon is running. Connect a hub first with [bold]pb connect[/].")
        return EXIT_DAEMON_UNAVAILABLE
    except pb_client.JsonRpcError as exc:
        return _report_hub_error(err, exc)

    err.print(f"Downloaded {size} bytes.")
    return EXIT_OK


# -------------------------------------------------------------------- daemon


@daemon_app.command(name="status")
def daemon_status(*, verbose: Verbose = 0, start: bool = False) -> int:
    """Show whether the daemon is running, and what it is doing.

    Args:
        verbose: Increase log detail.
        start: Start a daemon if none is running, instead of reporting that
            none is.
    """
    _configure_logging(verbose)
    return anyio.run(_daemon_status, start, backend="asyncio")


async def _daemon_status(start: bool) -> int:
    out, err = _console(), _console(err=True)
    try:
        async with pb_client.connect(autostart=start) as daemon:
            info = await daemon.info()
    except pb_client.DaemonUnavailable:
        err.print("[yellow]No daemon is running.[/]")
        err.print("Start one with [bold]pb daemon start[/].")
        return EXIT_DAEMON_UNAVAILABLE
    except pb_client.DaemonStartError as exc:
        err.print(f"[red]{exc}[/]")
        return EXIT_ERROR

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


@daemon_app.command(name="start")
def daemon_start(*, verbose: Verbose = 0) -> int:
    """Start the daemon if it is not already running.

    Args:
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return anyio.run(_daemon_start, backend="asyncio")


async def _daemon_start() -> int:
    err = _console(err=True)
    try:
        async with pb_client.connect(autostart=True) as daemon:
            info = await daemon.info()
    except pb_client.DaemonStartError as exc:
        err.print(f"[red]{exc}[/]")
        return EXIT_ERROR
    err.print(f"Daemon running (pid {info.pid}) on {info.endpoint}.")
    return EXIT_OK


@daemon_app.command(name="stop")
def daemon_stop(*, verbose: Verbose = 0) -> int:
    """Ask the daemon to exit.

    Args:
        verbose: Increase log detail.
    """
    _configure_logging(verbose)
    return anyio.run(_daemon_stop, backend="asyncio")


async def _daemon_stop() -> int:
    err = _console(err=True)
    try:
        async with pb_client.connect(autostart=False) as daemon:
            await daemon.shutdown()
    except pb_client.DaemonUnavailable:
        err.print("No daemon is running.")
        return EXIT_OK
    err.print("Daemon stopping.")
    return EXIT_OK


def main() -> int:
    """Run the CLI.

    Returns:
        The process exit code.
    """
    result = app()
    return result if isinstance(result, int) else EXIT_OK
