# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""An interactive session with the interpreter on a hub.

Four things happen at once and each is a task of its own: the hub's output goes
to the screen, the keyboard goes to the hub, a timer keeps the status line
honest about the window size, and the whole thing ends when any one of them
decides it should. They share one cancel scope, so whichever finishes first
takes the others with it -- there is no way for the session to half-end.

Two orderings in here are load-bearing rather than incidental. The terminal is
put into raw mode **last**, so it is restored **first**: anything printed while
the connection is being taken down lands on a terminal that works. And the hub
is attached to **before** the interpreter is started, so its greeting is
already being listened for when it arrives.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

import pb_client
from pb import banner as banner_module
from pb import console as console_module
from pb import errors, escape, ui
from pb.app import PROGRAM_COMMANDS, Verbose, app, configure_logging
from pb.errors import EXIT_HUB_ERROR, EXIT_OK
from pb.escape import Action

logger = logging.getLogger(__name__)

__all__ = ["repl_cmd"]

KEY_POLL_SECONDS = 0.01
"""How often to look for a keystroke.

Far below the speed anyone types, so a key never feels late, and a poll that
finds nothing costs one system call. This is a poll rather than a blocking read
because a blocking read of a console cannot be cancelled on either platform,
and a reader thread that outlives its session goes on eating the keys meant for
the next command.
"""

TICK_SECONDS = 0.25
"""How often to check for things nothing announces, such as a resized window."""

FLUSH_SECONDS = 2.0
"""How long leaving waits for what was already typed to reach the hub.

Typing a line and leaving in the same breath is an ordinary thing to do, and
throwing that line away because the session ended a few milliseconds before the
radio finished with it would be silently wrong. Bounded, because the other
possibility is a hub that has stopped reading, and then leaving has to win.
"""

STOP_SECONDS = 3.0
"""How long leaving waits for the hub to confirm the interpreter has stopped."""

INPUT_QUEUE = 256
"""Reads of the keyboard that may be waiting to reach the hub.

Keystrokes go to the hub through a queue rather than directly, so that a hub
which is slow to take input -- or has stopped taking it altogether -- cannot
stop ``pb`` from noticing the key that ends the session.
"""


@app.command(name="repl", group=PROGRAM_COMMANDS, sort_key=1)
async def repl_cmd(
    *, hub: str | None = None, replay: int = 0, verbose: Verbose = 0
) -> int:
    """Talk to a hub's interpreter, a line at a time.

    Everything typed goes to the hub, Ctrl-C included -- which is how a
    running program is interrupted. The one key that does not is Ctrl-],
    which introduces a command for pb itself: Ctrl-] q leaves, and Ctrl-] ?
    lists the rest.

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
    problem = console_module.why_unavailable()
    if problem is not None:
        return ui.usage(
            f"[bold]pb repl[/] needs a terminal, and {problem}.",
            "Run it in Windows Terminal, PowerShell or cmd."
            if problem.endswith("cannot do this)")
            else "Run it in a terminal rather than through a pipe.",
        )

    async with pb_client.connect(autostart=False) as daemon:
        info = await daemon.hub_info(hub)
        # Attached before the interpreter is started, so that its greeting is
        # already being listened for by the time it is printed.
        async with daemon.attach(hub_id=hub, replay_bytes=replay) as events:
            with console_module.raw_console() as console:
                return await _session(daemon, info, console, events, hub)


async def _session(
    daemon: pb_client.Daemon,
    info: pb_client.HubInfo,
    console: console_module.Console,
    events: AsyncIterator[pb_client.HubEvent],
    hub: str | None,
) -> int:
    """Run one session, returning when it ends.

    Args:
        daemon: The connected service.
        info: The hub being talked to.
        console: The terminal, already raw.
        events: Everything the hub says.
        hub: Which hub, as the user named it.

    Returns:
        The exit code: zero unless the hub went away.
    """
    ended = _Ending()
    keys = escape.Filter()
    sender, receiver = anyio.create_memory_object_stream[bytes](INPUT_QUEUE)

    with banner_module.Banner(console, info) as bar:
        bar.line(f"{escape.describe()} q to leave, {escape.describe()} ? for help.")
        # Before the tasks, not inside them, so that leaving in the same breath
        # as starting cannot race the decision about what to leave running.
        # Nothing is missed by starting it here: the hub was attached to before
        # this session began, so its greeting is already queued.
        started = await _start_if_idle(daemon, info, bar, hub)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_show_hub, events, bar, ended, tg.cancel_scope)
            tg.start_soon(_send_input, daemon, hub, receiver, bar, ended)
            tg.start_soon(
                _read_keys, daemon, hub, console, keys, sender, bar, ended, tg
            )
            tg.start_soon(_tick, bar)

        if started and not ended.disconnected:
            await _stop_what_we_started(daemon, hub, bar)
        elif started and ended.still_running:
            # Nothing can be sent to a hub that has gone, so whatever it was
            # doing, it is still doing -- and a hub with something running does
            # not advertise. Why it went is not knowable from here: switched
            # off, carried out of range, or its battery flat. Only the first of
            # those needs no rescuing, so the sentence says which case it is
            # about rather than asserting the hub is still on. The gentler fix
            # comes first: the hub's button stops the program, and only a hub
            # that will not answer that needs the power cycle.
            bar.line(
                "If the hub is still switched on, the interpreter is still "
                "running on it, and a hub with something running does not "
                "advertise. Press the hub's button to stop it, or switch the "
                "hub off and on."
            )

    if ended.disconnected:
        return EXIT_HUB_ERROR
    return EXIT_OK


async def _stop_what_we_started(
    daemon: pb_client.Daemon, hub: str | None, bar: banner_module.Banner
) -> None:
    """Stop the interpreter this session started, on the way out.

    A hub only advertises when nothing is running on it, and the interpreter is
    a running program as far as the hub is concerned. So a session that walks
    away and leaves it running leaves a hub that nothing can find -- not the
    next command, not another machine, not the user wondering why their hub has
    apparently switched itself off. Whoever started it puts it back.

    An interpreter this session merely joined, or a program somebody else
    started, is left alone: it was not ours to stop.

    Args:
        daemon: The connected service.
        hub: Which hub.
        bar: The status line, for saying what was done and why.
    """
    # Shielded and bounded: this runs while the session is already unwinding,
    # and a hub that will not answer must not hold the command open.
    with anyio.move_on_after(STOP_SECONDS, shield=True):
        try:
            await daemon.stop_program(hub)
        except pb_client.JsonRpcError as exc:
            logger.info("could not stop the interpreter: %s", exc)
            return
        bar.line("Interpreter stopped, so the hub can be found again.")
        return
    logger.info("the hub did not confirm the interpreter had stopped")


class _Ending:
    """How the session is going to stop, shared by the tasks that decide it."""

    def __init__(self) -> None:
        """Start out having ended for no reason yet."""
        self.disconnected = False
        """Set when the hub went away rather than the user leaving."""

        self.still_running = True
        """Whether the hub last said something was running on it.

        True to begin with, because this only matters to a session that
        started the interpreter, and it was running when it did. What it is
        for is the difference between a hub that was disconnected on purpose
        -- where the service stops a built-in program first, and the hub is
        back to advertising -- and one that stopped answering, where whatever
        was running still is.
        """

        self.flushed = anyio.Event()
        """Set once everything typed has reached the hub, or failed to."""


async def _start_if_idle(
    daemon: pb_client.Daemon,
    info: pb_client.HubInfo,
    bar: banner_module.Banner,
    hub: str | None,
) -> bool:
    """Start the interpreter, unless something is already running.

    A hub runs one thing at a time, so starting the interpreter stops whatever
    else was going. Doing that to a program somebody deliberately started
    would be a poor way to answer "let me look at this hub", and restarting an
    interpreter that is already running would throw away the session it holds.
    Both cases are therefore left alone and said out loud instead.

    Args:
        daemon: The connected service.
        info: The hub as it was when the session began.
        bar: The status line, for saying what was decided.
        hub: Which hub.

    Returns:
        True if this session started the interpreter, and so should stop it
        again on the way out.
    """
    if info.running_program_id == pb_client.REPL_PROGRAM_ID and info.program_running:
        bar.line("Joining the interpreter that is already running.")
        return False
    if info.program_running:
        bar.line(
            f"A program is running, so the interpreter was not started. "
            f"{escape.describe()} r starts it, stopping that program."
        )
        return False
    await daemon.start_repl(hub)
    return True


async def _show_hub(
    events: AsyncIterator[pb_client.HubEvent],
    bar: banner_module.Banner,
    ended: _Ending,
    scope: anyio.CancelScope,
) -> None:
    """Put everything the hub says on the screen, and notice when it stops.

    Args:
        events: The hub's output, status and state.
        bar: The status line and the screen above it.
        ended: Where to record that the hub went away.
        scope: The session's scope, cancelled when there is nothing left to
            listen to.
    """
    async for event in events:
        if isinstance(event, pb_client.Output):
            bar.write(event.data)
        elif isinstance(event, pb_client.Status):
            ended.still_running = event.program_running
            bar.update(event)
        elif not event.connected and event.state == "disconnected":
            bar.line(f"The hub disconnected ({event.reason or 'no reason given'}).")
            ended.disconnected = True

    # The stream only ends when the hub has gone, so there is nothing left to
    # type at.
    if not ended.disconnected:
        bar.line("The hub is no longer connected.")
        ended.disconnected = True
    scope.cancel()


async def _send_input(
    daemon: pb_client.Daemon,
    hub: str | None,
    keystrokes: MemoryObjectReceiveStream[bytes],
    bar: banner_module.Banner,
    ended: _Ending,
) -> None:
    """Pass what was typed to the hub, one queued read at a time.

    Its own task because a write can take a while: a hub whose input buffer is
    full makes the service wait for the program to read. Reading the keyboard
    must not wait behind that, or the key that ends the session would be the
    one key that could not get through.

    Args:
        daemon: The connected service.
        hub: Which hub.
        keystrokes: What has been typed and not yet sent.
        bar: The status line, for saying when the hub would not take it.
        ended: Where to report that the queue has run dry.
    """
    async with keystrokes:
        async for data in keystrokes:
            try:
                await daemon.write_stdin(data, hub_id=hub)
            except pb_client.JsonRpcError as exc:
                logger.info("the hub would not take input: %s", exc)
                bar.notice(str(exc))
    # Nothing more is coming and everything that came has gone, so leaving no
    # longer risks throwing away something that was typed.
    ended.flushed.set()


async def _read_keys(
    daemon: pb_client.Daemon,
    hub: str | None,
    console: console_module.Console,
    keys: escape.Filter,
    keystrokes: MemoryObjectSendStream[bytes],
    bar: banner_module.Banner,
    ended: _Ending,
    tg: anyio.abc.TaskGroup,
) -> None:
    """Read the keyboard, sorting keys for the hub from commands for us.

    Args:
        daemon: The connected service.
        hub: Which hub.
        console: The terminal.
        keys: The escape filter, which holds what a half-typed command needs.
        keystrokes: Where to put the bytes bound for the hub.
        bar: The status line.
        ended: How the session is ending.
        tg: The session's task group, cancelled when the user leaves.
    """
    try:
        await _keys_until_done(daemon, hub, console, keys, keystrokes, bar)
    finally:
        # Closed before waiting, so that what is queued is what gets sent and
        # the writer knows nothing more is coming.
        await keystrokes.aclose()

    with anyio.move_on_after(FLUSH_SECONDS):
        await ended.flushed.wait()
    tg.cancel_scope.cancel()


async def _keys_until_done(
    daemon: pb_client.Daemon,
    hub: str | None,
    console: console_module.Console,
    keys: escape.Filter,
    keystrokes: MemoryObjectSendStream[bytes],
    bar: banner_module.Banner,
) -> None:
    """Read the keyboard until the user asks to leave.

    Args:
        daemon: The connected service.
        hub: Which hub.
        console: The terminal.
        keys: The escape filter.
        keystrokes: Where to put the bytes bound for the hub.
        bar: The status line.
    """
    while True:
        typed = console.read()
        if not typed:
            await anyio.sleep(KEY_POLL_SECONDS)
            continue

        for_hub, actions = keys.feed(typed)
        for action in actions:
            if await _act(action, daemon, hub, bar):
                return
        if for_hub:
            try:
                keystrokes.send_nowait(for_hub)
            except anyio.WouldBlock:
                # The hub is far enough behind that hundreds of reads are
                # already waiting. Losing keys is bad; saying nothing about it
                # is worse.
                bar.notice("the hub is not keeping up; input was dropped")


async def _act(
    action: Action,
    daemon: pb_client.Daemon,
    hub: str | None,
    bar: banner_module.Banner,
) -> bool:
    """Carry out one escape command.

    Args:
        action: What was asked for.
        daemon: The connected service.
        hub: Which hub.
        bar: The status line and the screen above it.

    Returns:
        True if the session should end.
    """
    if action is Action.QUIT:
        return True
    if action is Action.STOP:
        await daemon.stop_program(hub)
        bar.notice("stopped")
    elif action is Action.RESTART:
        await daemon.start_repl(hub)
        bar.notice("restarted")
    elif action is Action.HELP:
        bar.line("")
        for keystrokes, what in escape.help_lines():
            bar.line(f"  {keystrokes:<16} {what}")
        bar.line("")
    else:
        bar.notice(f"no such command; {escape.describe()} ? for help")
    return False


async def _tick(bar: banner_module.Banner) -> None:
    """Keep the status line right about the things nothing announces.

    Args:
        bar: The status line.
    """
    while True:
        await anyio.sleep(TICK_SECONDS)
        bar.tick()
