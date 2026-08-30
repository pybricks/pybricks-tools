# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Managing the background service that keeps hubs connected.

Rarely needed: the service starts by itself when something wants it and stops
when nothing has for a while. These exist for the times it has to be looked at
directly -- after an upgrade, or when a hub is behaving oddly and the log is
the only place the reason will be.
"""

from __future__ import annotations

import logging

import pb_client
from pb import errors, ui
from pb.app import Verbose, configure_logging, service_app
from pb.errors import EXIT_OK

logger = logging.getLogger(__name__)

__all__ = ["service_start", "service_status", "service_stop"]

_HANDLED_HERE = (pb_client.PbClientError, BaseExceptionGroup)
"""What to catch when a command deals with a failure itself.

Catching the error's own type is not enough: anything raised inside an
``async with pb_client.connect()`` block comes back wrapped in a group of one,
so the type to catch is not the type that was raised.
"""


@service_app.command(name="status")
async def service_status(*, verbose: Verbose = 0, start: bool = False) -> int:
    """Show whether the background service is running.

    Args:
        verbose: Increase log detail.
        start: Start the service if it is not running, instead of reporting
            that it is not.
    """
    configure_logging(verbose)
    return await errors.run(_service_status, start)


async def _service_status(start: bool) -> int:
    out, err = ui.console(), ui.console(err=True)
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

    ui.rows(
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
async def service_start(*, verbose: Verbose = 0) -> int:
    """Start the background service if it is not already running.

    Args:
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_service_start)


async def _service_start() -> int:
    err = ui.console(err=True)
    async with pb_client.connect(autostart=True) as daemon:
        info = await daemon.info()
    err.print(f"Running (pid {info.pid}).")
    return EXIT_OK


@service_app.command(name="stop")
async def service_stop(*, verbose: Verbose = 0) -> int:
    """Stop the background service, disconnecting any hubs.

    Args:
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_service_stop)


async def _service_stop() -> int:
    err = ui.console(err=True)
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
