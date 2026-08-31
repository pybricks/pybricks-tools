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
from pb_cli import errors, session, ui
from pb_cli.app import PROGRAM_COMMANDS, Verbose, app, configure_logging
from pb_cli.errors import EXIT_OK
from pb_core import project
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
    wait: bool | None = None,
    verbose: Verbose = (),
) -> int:
    """Compile a script, send it to a hub, and run it.

    The command stays with the program: its output appears as it is printed,
    and on a terminal the keyboard goes to the hub, so Ctrl-C interrupts and
    input() reads what you type. Ctrl-] q leaves; Ctrl-] h lists the rest.
    Without a terminal -- in a pipeline or a build -- the output still comes
    through, on stdout.

    Args:
        script: The Python file to run. Leave it out when using --command.
        hub: Which hub, by name. Defaults to the one in use.
        command: Python code to run, instead of a file. Like python -c.
        slot: Which program slot to use.
        wait: How long to stay with the program, and how much of the terminal
            to use while doing it. Left out, the session takes the terminal
            when there is one. --wait stays but leaves the terminal alone and
            only prints the output, which is what a script wants. --no-wait
            starts the program and returns, for setting a robot going and
            walking away.
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
    wait: bool | None,
) -> int:
    err = ui.console(err=True)
    compiled: pb_client.CompiledProgram | None = None
    program = b""
    with ui.spinner(err, "Compiling..."):
        if script is not None:
            roots = await project.module_roots(script)
            compiled = await pb_client.compile_program(script, roots=roots)
        else:
            # Exactly one of the two is given; the command line was checked
            # before any of this ran.
            assert command is not None
            program = await pb_client.compile_source(command)

    # Sending and running are two steps to the user, and were one spinner
    # that said "Downloading" for as long as the program ran.
    async with pb_client.connect(autostart=False) as service:
        if compiled is not None:
            program = compiled.pack(legacy_main=await _legacy_main(service, hub))
        with ui.spinner(err, f"Downloading {len(program)} bytes..."):
            await service.download(program, hub_id=hub, slot=slot)
        if wait is not False:
            # The hub stays connected either way, so there is nothing to be
            # gained by watching a spinner instead of the program. Asking for
            # --wait by name is asking for less than that: it says stay, and
            # says nothing about wanting the terminal taken over, so it is
            # taken to mean the plain form.
            return await _stay_with_it(service, hub, slot, terminal=wait is None)
        with ui.spinner(err, "Starting..."):
            await service.start_program(hub_id=hub, slot=slot, wait=False)

    err.print("Program started.")
    return EXIT_OK


async def _legacy_main(service: pb_client.Service, hub: str | None) -> bool:
    """Ask the hub whether the program's entry module must be called __main__.

    Args:
        service: The connected service.
        hub: Which hub, by name, or None for the one in use.

    Returns:
        True for a hub on a Pybricks profile before v1.5.0.

    This is why compiling and packing are separate: the name depends on the
    hub, and compiling happens before anything is connected to, so that a
    syntax error is reported without a hub having to be found first.
    """
    info = await service.hub_info(hub_id=hub)
    return info.legacy_main_module


@app.command(name="download", group=PROGRAM_COMMANDS, sort_key=2)
async def download_cmd(
    script: Path,
    /,
    *,
    hub: str | None = None,
    slot: int | None = None,
    verbose: Verbose = (),
) -> int:
    """Compile a script and send it to a hub without running it.

    A program goes into whichever slot the hub has selected -- the hub
    decides, and its buttons are what change the selection. Naming a slot here
    says which one you expect, and the download is refused rather than put
    somewhere else if the hub disagrees.

    Args:
        script: The Python file to send.
        hub: Which hub, by name. Defaults to the one in use.
        slot: The slot it should land in. Defaults to whichever the hub has
            selected.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_download, script, hub, slot)


async def _download(script: Path, hub: str | None, slot: int | None) -> int:
    err = ui.console(err=True)
    roots = await project.module_roots(script)
    compiled = await pb_client.compile_program(script, roots=roots)

    async with pb_client.connect(autostart=False) as service:
        program = compiled.pack(legacy_main=await _legacy_main(service, hub))
        written = await service.download(program, hub_id=hub, slot=slot)

    # Which slot is only worth saying on a hub that has more than one, where
    # it is also the thing most worth saying: it is the hub's choice, not
    # ours, and the answer to "where did my program go?".
    where = f" into slot {written.slot}" if written.slots > 1 else ""
    err.print(f"Downloaded {written.size} bytes{where}.")
    return EXIT_OK


@app.command(name="start", group=PROGRAM_COMMANDS, sort_key=3)
async def start_cmd(
    *,
    hub: str | None = None,
    slot: int | None = None,
    wait: bool | None = None,
    verbose: Verbose = (),
) -> int:
    """Start a program that is already on the hub.

    Nothing is sent to the hub, so this is the quick way to run the same
    program again, and the way to run one put there by another tool. What
    happens after it starts is exactly what happens for pb run.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        slot: Which program slot. Defaults to the one the hub has selected.
        wait: How long to stay with the program, and how much of the terminal
            to use while doing it. Left out, the session takes the terminal
            when there is one and returns straight away when there is not,
            since a script starting a stored program is setting a robot going.
            --wait stays either way, leaving the terminal alone and printing
            only the output. --no-wait always returns as soon as it starts.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_start, hub, slot, wait, hints=_START_HINTS)


async def _start(hub: str | None, slot: int | None, wait: bool | None) -> int:
    err = ui.console(err=True)
    if wait is None and not session.have_terminal():
        # The one place this differs from pb run. Starting a program that is
        # already on the hub, from something that is not a terminal, is what a
        # script does to set a robot going -- so it starts it and returns,
        # rather than holding the script open for as long as the robot runs.
        # At a terminal the opposite is true: somebody typing pb start wants
        # to see what it does.
        wait = False
    async with pb_client.connect(autostart=False) as service:
        if wait is not False:
            return await _stay_with_it(service, hub, slot, terminal=wait is None)
        await service.start_program(hub_id=hub, slot=slot)
    err.print("Program started.")
    return EXIT_OK


async def _stay_with_it(
    service: pb_client.Service, hub: str | None, slot: int | None, *, terminal: bool
) -> int:
    """Start a program and stay with it until it ends.

    The same session as ``pb repl``, with a program on the other end instead
    of the interpreter: output as it is printed, the keyboard going to the
    hub, and the status line along the bottom. It ends when the program does,
    or when the user asks to leave -- and a program this command started is
    stopped on the way out, since a hub with something running cannot be found
    by anything.

    Where there is no terminal, or where the caller would rather it were left
    alone, the same thing happens with the terminal parts left out: the output
    still arrives, on stdout, and the exit code still says how the program
    ended. That is what a build or a pipeline gets.

    Args:
        service: The connected service.
        hub: Which hub.
        slot: Which program slot, or None for the hub's own choice.
        terminal: Whether taking the terminal over is wanted at all. A script
            run from a terminal is still a script.

    Returns:
        The exit code: three if the program ended in an exception, since a
        program that failed in front of the user is not a command that
        succeeded.
    """
    info = await service.hub_info(hub)
    running = session.Running("program", watch_for_crash=True)

    async def start(announce: session.Announce) -> bool:
        """Set the program going, now that its output is being listened for.

        Args:
            announce: Says one line to the user. Unused: the program speaks
                for itself.

        Returns:
            True, always: this command is what started it, so leaving is what
            has to stop it.
        """
        del announce
        await service.start_program(hub_id=hub, slot=slot, wait=False)
        return True

    # Both have to hold: something to take over, and a reason to.
    use_terminal = terminal and session.have_terminal()
    stay = session.attached if use_terminal else session.streamed
    return await stay(service, info, hub, replay=0, running=running, start=start)


@app.command(name="stop", group=PROGRAM_COMMANDS, sort_key=4)
async def stop_cmd(*, hub: str | None = None, verbose: Verbose = ()) -> int:
    """Stop the program running on a hub.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_stop, hub)


async def _stop(hub: str | None) -> int:
    err = ui.console(err=True)
    async with pb_client.connect(autostart=False) as service:
        await service.stop_program(hub)
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
    verbose: Verbose = (),
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
        roots = await project.module_roots(script)
        # Packed with the modern naming, which nothing here depends on: no
        # program is being sent, so the entry module's name is never read.
        program = await pb_client.compile_script(script, roots=roots)
        named = script.name
    else:
        assert command is not None
        program = await pb_client.compile_source(command)
        named = pb_client.SOURCE_NAME
    # A failure needs no branch here: the compiler's own diagnostics come back
    # as a CompileError, and errors.run prints them under one plain sentence.
    err.print(f"[green]{named} compiles.[/] {len(program)} bytes.")
    return EXIT_OK
