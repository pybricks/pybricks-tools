# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Getting a program onto a hub and controlling it once it is there.

Compiling happens here rather than in the service, because the script is on
this machine's filesystem and the service may not be.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import cyclopts

import pb_client
from pb import errors, ui
from pb.app import PROGRAM_COMMANDS, Verbose, app, configure_logging
from pb.errors import EXIT_HUB_ERROR, EXIT_OK
from pb_core.jsonrpc import ErrorCode

logger = logging.getLogger(__name__)

__all__ = ["download_cmd", "run_cmd", "start_cmd", "stop_cmd"]

_START_HINTS = {
    ErrorCode.HUB_INVALID_COMMAND: (
        "There may be no program on the hub yet. Send one with "
        "[bold]pb run[/] or [bold]pb download[/]."
    ),
}
"""What a refused start usually means.

The hub answers "invalid command" both for a slot with nothing in it and for
a program its firmware cannot run, and the first is far more likely.
"""


@app.command(name="run", group=PROGRAM_COMMANDS, sort_key=0)
async def run_cmd(
    script: Path | None = None,
    /,
    *,
    command: Annotated[
        str | None, cyclopts.Parameter(name=["-c", "--command"], show_default=False)
    ] = None,
    hub: str | None = None,
    slot: int | None = None,
    wait: bool = True,
    verbose: Verbose = 0,
) -> int:
    """Compile a script, send it to a hub, and run it.

    Args:
        script: The Python file to run. Leave it out when using --command.
        hub: Which hub, by name. Defaults to the one in use.
        command: Python code to run, instead of a file. Like python -c.
        slot: Which program slot to use.
        wait: Wait for the program to finish before returning.
        verbose: Increase log detail.
    """
    if (script is None) == (command is None):
        return ui.usage(
            "Give a script to run, or -c with the code to run.",
            "For example [bold]pb run demo.py[/] or [bold]pb run -c 'print(1)'[/].",
        )
    configure_logging(verbose)
    return await errors.run(_run, script, command, hub, slot, wait)


async def _run(
    script: Path | None,
    command: str | None,
    hub: str | None,
    slot: int | None,
    wait: bool,
) -> int:
    err = ui.console(err=True)

    with ui.spinner(err, "Compiling..."):
        if script is not None:
            program = await pb_client.compile_script(script)
        else:
            # Exactly one of the two is given; the command line was checked
            # before any of this ran.
            assert command is not None
            program = await pb_client.compile_source(command)

    # Sending and running are two steps to the user, and were one spinner
    # that said "Downloading" for as long as the program ran.
    async with pb_client.connect(autostart=False) as daemon:
        with ui.spinner(err, f"Downloading {len(program)} bytes..."):
            await daemon.download(program, hub_id=hub)
        with ui.spinner(err, "Running..." if wait else "Starting..."):
            reason = await daemon.start_program(hub_id=hub, slot=slot, wait=wait)

    if reason == "disconnected":
        err.print("[yellow]The hub disconnected while the program was running.[/]")
        return EXIT_HUB_ERROR
    if reason == "started":
        err.print("Program started.")
    else:
        err.print("Program finished.")
    return EXIT_OK


@app.command(name="download", group=PROGRAM_COMMANDS, sort_key=1)
async def download_cmd(
    script: Path, /, *, hub: str | None = None, verbose: Verbose = 0
) -> int:
    """Compile a script and send it to a hub without running it.

    Args:
        script: The Python file to send.
        hub: Which hub, by name. Defaults to the one in use.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_download, script, hub)


async def _download(script: Path, hub: str | None) -> int:
    err = ui.console(err=True)
    program = await pb_client.compile_script(script)

    async with pb_client.connect(autostart=False) as daemon:
        size = await daemon.download(program, hub_id=hub)

    err.print(f"Downloaded {size} bytes.")
    return EXIT_OK


@app.command(name="start", group=PROGRAM_COMMANDS, sort_key=2)
async def start_cmd(
    *, hub: str | None = None, slot: int | None = None, verbose: Verbose = 0
) -> int:
    """Start a program that is already on the hub.

    Nothing is sent to the hub, so this is the quick way to run the same
    program again, and the way to run one put there by another tool.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        slot: Which program slot. Defaults to the one the hub has selected.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_start, hub, slot, hints=_START_HINTS)


async def _start(hub: str | None, slot: int | None) -> int:
    err = ui.console(err=True)
    async with pb_client.connect(autostart=False) as daemon:
        await daemon.start_program(hub_id=hub, slot=slot)
    err.print("Program started.")
    return EXIT_OK


@app.command(name="stop", group=PROGRAM_COMMANDS, sort_key=3)
async def stop_cmd(*, hub: str | None = None, verbose: Verbose = 0) -> int:
    """Stop the program running on a hub.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_stop, hub)


async def _stop(hub: str | None) -> int:
    err = ui.console(err=True)
    async with pb_client.connect(autostart=False) as daemon:
        await daemon.stop_program(hub)
    err.print("Stopped.")
    return EXIT_OK
