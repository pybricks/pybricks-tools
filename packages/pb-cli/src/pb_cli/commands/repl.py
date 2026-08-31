# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""An interactive session with the interpreter on a hub.

The session itself lives in :mod:`pb_cli.session`, which is shared with ``pb run``
and ``pb start``. What belongs here is only what is particular to talking to
the interpreter: deciding whether to start one, join one, or refuse.
"""

from __future__ import annotations

import logging

import pb_client
from pb_cli import errors, session, ui
from pb_cli.app import PROGRAM_COMMANDS, Verbose, app, configure_logging
from pb_cli.errors import EXIT_HUB_ERROR

logger = logging.getLogger(__name__)

__all__ = ["repl_cmd"]


@app.command(name="repl", group=PROGRAM_COMMANDS, sort_key=1)
async def repl_cmd(
    *, hub: str | None = None, replay: int = 0, verbose: Verbose = ()
) -> int:
    """Talk to a hub's interpreter, a line at a time.

    Everything typed goes to the hub, Ctrl-C included -- which is how a
    running program is interrupted. The one key that does not is Ctrl-],
    which introduces a command for pb itself: Ctrl-] q leaves, and Ctrl-] h
    lists the rest.

    Ctrl-D ends the interpreter, and so ends the session. A hub already busy
    with a program of its own is left alone rather than interrupted.

    A status line along the bottom says which hub is being talked to, whether
    anything is listening, and whether its battery is about to end the
    conversation.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        replay: How much earlier output to show first, in bytes. Useful for
            joining a program that is already running and printing.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_repl, hub, replay)


async def _repl(hub: str | None, replay: int) -> int:
    no_terminal = session.needs_terminal("pb repl")
    if no_terminal is not None:
        return no_terminal

    async with pb_client.connect(autostart=False) as daemon:
        info = await daemon.hub_info(hub)
        busy = _refuse_if_busy(info)
        if busy is not None:
            return busy

        joining = _is_the_interpreter(info)
        running = session.Running("interpreter", arrived=joining)

        async def start(announce: session.Announce) -> None:
            """Start the interpreter, or join the one already running.

            Restarting an interpreter that is already going would throw away
            the session it holds, so an existing one is joined instead -- and
            left running afterwards, since it was not ours to stop.

            Args:
                announce: Says one line to the user.
            """
            if joining:
                announce("Joining the interpreter that is already running.")
                return
            await daemon.start_repl(hub)
            running.ours = True

        return await session.attached(
            daemon, info, hub, replay=replay, running=running, start=start
        )


def _is_the_interpreter(info: pb_client.HubInfo) -> bool:
    """Whether what the hub is running is its own interpreter.

    Args:
        info: The hub as it was when the session was asked for.

    Returns:
        True if there is an interpreter to join.
    """
    return bool(
        info.program_running and info.running_program_id == pb_client.REPL_PROGRAM_ID
    )


def _refuse_if_busy(info: pb_client.HubInfo) -> int | None:
    """Refuse the session if the hub is busy with a program of its own.

    A hub runs one thing at a time, so starting the interpreter would stop
    whatever is running -- a poor way to answer "let me look at this hub".
    Nor is there anything that could put things back afterwards: there is no
    equivalent of CPython's ``-i`` here, so a program stopped to make room for
    the interpreter cannot be handed back to.

    Attaching and waiting for the program to end was the other possibility,
    and is worse than refusing. It looks exactly like a session that has hung,
    and the one thing the user asked for -- a prompt -- is the one thing it
    cannot give them.

    Args:
        info: The hub as it was when the session was asked for.

    Returns:
        The exit code to use, or ``None`` if there is nothing in the way.
    """
    if not info.program_running or _is_the_interpreter(info):
        return None
    return errors.render(
        ui.console(err=True),
        [
            errors.Problem(
                "A program is running on the hub, so the interpreter cannot start.",
                "Wait for it to finish, or stop it with [bold]pb stop[/] or "
                "the hub's button.",
                exit_code=EXIT_HUB_ERROR,
            )
        ],
    )
