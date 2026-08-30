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
from pb import banner as banner_module
from pb import errors, session, ui
from pb.app import PROGRAM_COMMANDS, Verbose, app, configure_logging
from pb.errors import EXIT_HUB_ERROR, EXIT_OK
from pb_core.jsonrpc import ErrorCode

logger = logging.getLogger(__name__)

__all__ = ["check_cmd", "download_cmd", "run_cmd", "start_cmd", "stop_cmd"]

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
    interactive: Annotated[
        bool, cyclopts.Parameter(name=["-i", "--interactive"])
    ] = False,
    verbose: Verbose = 0,
) -> int:
    """Compile a script, send it to a hub, and run it.

    With --interactive the program's output appears as it is printed and the
    keyboard goes to the hub, which is what a program using input() or hub
    buttons needs. Ctrl-] q leaves; Ctrl-] h lists the rest.

    Args:
        script: The Python file to run. Leave it out when using --command.
        hub: Which hub, by name. Defaults to the one in use.
        command: Python code to run, instead of a file. Like python -c.
        slot: Which program slot to use.
        wait: Wait for the program to finish before returning.
        interactive: Stay with the program, showing its output and passing
            the keyboard to it.
        verbose: Increase log detail.
    """
    if (script is None) == (command is None):
        return ui.usage(
            "Give a script to run, or -c with the code to run.",
            "For example [bold]pb run demo.py[/] or [bold]pb run -c 'print(1)'[/].",
        )
    if interactive and not wait:
        # Two answers to the same question -- how long to stay with the
        # program -- and they disagree.
        return ui.usage(
            "--interactive stays with the program, so it cannot be combined "
            "with --no-wait.",
            "Leave out [bold]--no-wait[/] to watch it, or [bold]--interactive[/] "
            "to start it and return.",
        )
    configure_logging(verbose)
    return await errors.run(_run, script, command, hub, slot, wait, interactive)


async def _run(
    script: Path | None,
    command: str | None,
    hub: str | None,
    slot: int | None,
    wait: bool,
    interactive: bool,
) -> int:
    err = ui.console(err=True)
    if interactive:
        # Before compiling: refusing after a wait, for a reason known before
        # it started, is a poor way to treat somebody's time.
        no_terminal = session.needs_terminal("pb run --interactive")
        if no_terminal is not None:
            return no_terminal

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
        if interactive:
            return await _interactively(daemon, hub, slot)
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


@app.command(name="download", group=PROGRAM_COMMANDS, sort_key=2)
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


@app.command(name="start", group=PROGRAM_COMMANDS, sort_key=3)
async def start_cmd(
    *,
    hub: str | None = None,
    slot: int | None = None,
    interactive: Annotated[
        bool, cyclopts.Parameter(name=["-i", "--interactive"])
    ] = False,
    verbose: Verbose = 0,
) -> int:
    """Start a program that is already on the hub.

    Nothing is sent to the hub, so this is the quick way to run the same
    program again, and the way to run one put there by another tool.

    With --interactive the command stays with the program, showing its output
    and passing the keyboard to it, instead of returning as soon as it starts.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        slot: Which program slot. Defaults to the one the hub has selected.
        interactive: Stay with the program, showing its output and passing
            the keyboard to it.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_start, hub, slot, interactive, hints=_START_HINTS)


async def _start(hub: str | None, slot: int | None, interactive: bool) -> int:
    err = ui.console(err=True)
    if interactive:
        no_terminal = session.needs_terminal("pb start --interactive")
        if no_terminal is not None:
            return no_terminal
    async with pb_client.connect(autostart=False) as daemon:
        if interactive:
            return await _interactively(daemon, hub, slot)
        await daemon.start_program(hub_id=hub, slot=slot)
    err.print("Program started.")
    return EXIT_OK


async def _interactively(
    daemon: pb_client.Daemon, hub: str | None, slot: int | None
) -> int:
    """Start a program and stay with it, on the terminal.

    The same session as ``pb repl``, with a program on the other end instead
    of the interpreter: output as it is printed, the keyboard going to the
    hub, and the status line along the bottom. It ends when the program does,
    or when the user asks to leave -- and a program this command started is
    stopped on the way out, since a hub with something running cannot be found
    by anything.

    Args:
        daemon: The connected service.
        hub: Which hub.
        slot: Which program slot, or None for the hub's own choice.

    Returns:
        The exit code.
    """
    info = await daemon.hub_info(hub)
    running = session.Running("program")

    async def start(bar: banner_module.Banner) -> None:
        """Set the program going, now that the screen is ready for it.

        Args:
            bar: The status line, unused: the program speaks for itself.
        """
        del bar
        await daemon.start_program(hub_id=hub, slot=slot, wait=False)
        running.ours = True

    return await session.attached(
        daemon, info, hub, replay=0, running=running, start=start
    )


@app.command(name="stop", group=PROGRAM_COMMANDS, sort_key=4)
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


@app.command(name="check", group=PROGRAM_COMMANDS, sort_key=5)
async def check_cmd(
    script: Path | None = None,
    /,
    *,
    command: Annotated[
        str | None, cyclopts.Parameter(name=["-c", "--command"], show_default=False)
    ] = None,
    verbose: Verbose = 0,
) -> int:
    """Check that a program compiles, without sending it anywhere.

    No hub and no background service: this is the compiler on its own. Useful
    while writing a program with no hub to hand, and as the fast check to put
    in a commit hook or a build, where starting a Bluetooth connection to find
    out about a missing bracket would be absurd.

    It reports what the compiler said and how big the program came out, since
    size is the other thing that can stop a program running.

    Args:
        script: The Python file to check. Leave it out when using --command.
        command: Python code to check, instead of a file. Like python -c.
        verbose: Increase log detail.
    """
    if (script is None) == (command is None):
        return ui.usage(
            "Give a script to check, or -c with the code to check.",
            "For example [bold]pb check demo.py[/] or [bold]pb check -c 'print(1)'[/].",
        )
    configure_logging(verbose)
    return await errors.run(_check, script, command)


async def _check(script: Path | None, command: str | None) -> int:
    err = ui.console(err=True)
    if script is not None:
        program = await pb_client.compile_script(script)
        named = script.name
    else:
        assert command is not None
        program = await pb_client.compile_source(command)
        named = pb_client.SOURCE_NAME
    # A failure needs no branch here: the compiler's own diagnostics come back
    # as a CompileError, and errors.run prints them under one plain sentence.
    err.print(f"[green]{named} compiles.[/] {len(program)} bytes.")
    return EXIT_OK
