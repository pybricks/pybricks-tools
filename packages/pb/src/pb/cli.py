# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The ``pb`` command line."""

from __future__ import annotations

import logging
import sys
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
EXIT_DAEMON_UNAVAILABLE = 4

app = cyclopts.App(
    name="pb",
    version=__version__,
    help="Talk to Pybricks hubs.\n\n"
    "A background daemon holds hub connections open, so a hub stays connected "
    "between commands.",
)

daemon_app = cyclopts.App(name="daemon", help="Manage the background daemon.")
app.command(daemon_app)


def _console(*, err: bool = False) -> Console:
    """Build a console.

    Data goes to stdout and everything else to stderr, so that piping output
    into another program stays clean.
    """
    return Console(stderr=err)


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


Verbose = Annotated[
    int,
    cyclopts.Parameter(
        name=["-v", "--verbose"],
        help="Increase log detail. Repeat for more (-v, -vv, -vvv).",
        show_default=False,
    ),
]


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

    if out.is_terminal:
        table = Table(show_header=False, box=None)
        table.add_row("status", "[green]running[/]")
        table.add_row("version", info.version)
        table.add_row("protocol", str(info.protocol_version))
        table.add_row("pid", str(info.pid))
        table.add_row("uptime", f"{info.uptime_s:.1f}s")
        table.add_row("endpoint", info.endpoint)
        table.add_row("clients", str(info.clients))
        table.add_row("log", info.log_file)
        out.print(table)
    else:
        for key, value in (
            ("status", "running"),
            ("version", info.version),
            ("protocol", info.protocol_version),
            ("pid", info.pid),
            ("uptime_s", f"{info.uptime_s:.1f}"),
            ("endpoint", info.endpoint),
            ("clients", info.clients),
            ("log", info.log_file),
        ):
            out.print(f"{key}\t{value}", highlight=False, markup=False)
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
