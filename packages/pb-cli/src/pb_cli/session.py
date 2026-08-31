# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""An interactive session with whatever is running on a hub.

The same terminal, whether what is on the other end is the hub's interpreter or
a program that was just sent to it. Four things happen at once and each is a
task of its own: the hub's output goes to the screen, the keyboard goes to the
hub, a timer keeps the status line honest about the window size, and the whole
thing ends when any one of them decides it should. They share one cancel scope,
so whichever finishes first takes the others with it -- there is no way for the
session to half-end.

Two orderings in here are load-bearing rather than incidental. The terminal is
put into raw mode **last**, so it is restored **first**: anything printed while
the connection is being taken down lands on a terminal that works. And the hub
is attached to **before** anything is started on it, so the first thing it
prints is already being listened for when it arrives.

What differs between a session with the interpreter and a session with a
program is only what starts it and what it is called, both of which the caller
supplies. Everything else -- the escape key, the status line, stopping what we
started on the way out, and noticing that the hub has gone -- is the same
session either way, which is the reason this is a module of its own rather than
part of ``pb repl``.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

import pb_client
from pb_cli import banner as banner_module
from pb_cli import console as console_module
from pb_cli import escape, ui
from pb_cli.errors import EXIT_HUB_ERROR, EXIT_INTERRUPTED, EXIT_OK
from pb_cli.escape import Action

logger = logging.getLogger(__name__)

__all__ = ["Running", "attached", "have_terminal", "needs_terminal", "streamed"]

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
"""How long leaving waits for the hub to confirm that it has stopped."""

INPUT_QUEUE = 256
"""Reads of the keyboard that may be waiting to reach the hub.

Keystrokes go to the hub through a queue rather than directly, so that a hub
which is slow to take input -- or has stopped taking it altogether -- cannot
stop ``pb`` from noticing the key that ends the session.
"""

Announce = Callable[[str], None]
"""Says one line to the user, wherever this session's lines go."""

StartFunc = Callable[[Announce], Awaitable[None]]
"""Starts whatever the session is here to talk to, once the screen is ready."""

TRACEBACK = b"Traceback (most recent call last):"
"""How the hub begins the report of a program that ended in an exception.

Nothing in the protocol says whether a program finished or failed -- the status
report only stops saying that something is running -- so the only evidence is
what the firmware prints. ``print_final_exception`` prints a traceback for an
unhandled exception, "The program was stopped (SystemExit)." for a deliberate
stop, and nothing at all for a program that simply ended.

Matching on what the hub printed is a heuristic and is worth naming as one. It
is here because the alternative is worse: a command that ran a program which
died with an exception, in front of the user, and then reported success.

All of this would go away if the hub reported how a program ended -- see
"Wanted from the firmware" in the design notes. Until then this is the only
evidence there is.
"""

INTERRUPT = b"KeyboardInterrupt"
"""The exception a program ends with when somebody pressed Ctrl-C at it.

Worth telling apart from every other exception, because it is not a fault in
the program: it is what the user asked for. CPython exits 130 for it and 1 for
anything else, and this follows that.
"""

TAIL_BYTES = 4096
"""How much of the end of a program's output to keep, to see how it ended.

Enough for a traceback with several frames, and small enough that one printed
earlier and recovered from has scrolled out of it by the time the program ends.
"""


class Running:
    """What the session knows about the thing it is talking to.

    The hub runs one thing at a time and says so in every status report, which
    makes this the answer to both of the questions a session keeps asking: is
    stopping it on the way out ours to do, and is there still anything on the
    other end.
    """

    def __init__(
        self, noun: str, *, arrived: bool = False, watch_for_crash: bool = False
    ) -> None:
        """Describe what the session expects to find running.

        Args:
            noun: What to call it in messages, such as ``"interpreter"``.
            arrived: True if it is known to be running already, which is the
                case for a session joining something rather than starting it.
            watch_for_crash: Whether ending in an exception should make the
                command fail. True for a program, whose failure is the
                command's failure; false for the interpreter, where an
                exception is an ordinary thing to type.
        """
        self.noun = noun
        """What this is called when the session has something to say about it."""

        self.ours = False
        """Whether this session started it, and so should stop it again."""

        self.arrived = arrived
        """Whether it has been seen running during this session.

        Until it has, a hub reporting nothing running is the moment before the
        start command took effect rather than the end of the session.
        """

        self.running = True
        """Whether the hub last said anything was running on it.

        True to begin with: a session only gets this far with something either
        started or already going.
        """

        self.gone = False
        """Whether it ran and has since stopped."""

        self._watch_for_crash = watch_for_crash
        self._tail = bytearray()
        """The end of what it printed, which is where it says how it ended."""

    def saw(self, data: bytes) -> None:
        """Take in some of what it printed.

        Args:
            data: The bytes, exactly as the hub sent them.
        """
        if not self._watch_for_crash:
            return
        self._tail.extend(data)
        del self._tail[:-TAIL_BYTES]

    def _final_exception(self) -> bytes:
        """Name the exception the hub last reported, if it reported one.

        Returns:
            The line naming it, such as ``b"ZeroDivisionError: divide by
            zero"``, or empty if the tail holds no traceback.
        """
        if not self._watch_for_crash:
            return b""
        start = self._tail.rfind(TRACEBACK)
        if start < 0:
            return b""
        # A traceback's frames are indented and the exception that ends it is
        # not, so the first unindented line after the header is the answer.
        after = bytes(self._tail[start + len(TRACEBACK) :])
        for line in after.splitlines():
            if line and not line.startswith((b" ", b"\t")):
                return line
        return b""

    @property
    def crashed(self) -> bool:
        """Whether it ended by raising something nothing caught."""
        ending = self._final_exception()
        return bool(ending) and not ending.startswith(INTERRUPT)

    @property
    def interrupted(self) -> bool:
        """Whether what ended it was Ctrl-C rather than a fault of its own."""
        return self._final_exception().startswith(INTERRUPT)

    def note(self, status: pb_client.Status) -> None:
        """Take in what the hub says about itself.

        Args:
            status: The hub's latest report.
        """
        was_running = self.running
        self.running = status.program_running
        if status.program_running:
            self.arrived = True
        elif was_running and self.arrived:
            self.gone = True


def have_terminal() -> bool:
    """Whether there is a terminal to run a session on.

    Returns:
        True if one is available. Asked rather than assumed, because staying
        with the program is what ``pb run`` does by default and the same
        command has to keep working in a pipeline, a build or an editor pane.
    """
    return console_module.why_unavailable() is None


def needs_terminal(command: str) -> int | None:
    """Refuse a session that has nowhere to draw itself.

    Args:
        command: What the user typed, for the message.

    Returns:
        An exit code if there is no usable terminal, or ``None`` if there is.
    """
    problem = console_module.why_unavailable()
    if problem is None:
        return None
    # No markup in the message: an error's headline is printed literally,
    # because it can carry a hub's name or a file path and a stray bracket in
    # one of those would either vanish or colour the rest of the line.
    # Suggestions are our own words, so those may still use it.
    return ui.usage(
        f"{command} needs a terminal, and {problem}.",
        "Run it in Windows Terminal, PowerShell or cmd."
        if problem.endswith("cannot do this)")
        else "Run it in a terminal rather than through a pipe.",
    )


async def attached(
    service: pb_client.Service,
    info: pb_client.HubInfo,
    hub: str | None,
    *,
    replay: int,
    running: Running,
    start: StartFunc,
) -> int:
    """Attach to a hub, take the terminal, and run one session.

    Args:
        service: The connected service.
        info: The hub being talked to.
        hub: Which hub, as the user named it.
        replay: How much earlier output to show first, in bytes.
        running: What the session expects to be talking to.
        start: Starts it, once the screen is ready to show what it says.

    Returns:
        The exit code: zero unless the hub went away or the program failed.
    """
    # Attached before anything is started, so that the first thing it prints is
    # already being listened for by the time it arrives.
    async with service.attach(hub_id=hub, replay_bytes=replay) as events:
        with console_module.raw_console() as console:
            return await _session(service, info, console, events, hub, running, start)


async def _session(
    service: pb_client.Service,
    info: pb_client.HubInfo,
    console: console_module.Console,
    events: AsyncIterator[pb_client.HubEvent],
    hub: str | None,
    running: Running,
    start: StartFunc,
) -> int:
    """Run one session, returning when it ends.

    Args:
        service: The connected service.
        info: The hub being talked to.
        console: The terminal, already raw.
        events: Everything the hub says.
        hub: Which hub, as the user named it.
        running: What the session is talking to.
        start: Starts it, inside the banner and before the tasks.

    Returns:
        The exit code: zero unless the hub went away or the program failed.
    """
    ended = _Ending()
    keys = escape.Filter()
    sender, receiver = anyio.create_memory_object_stream[bytes](INPUT_QUEUE)

    with banner_module.Banner(console, info) as bar:
        bar.line(f"{escape.describe()} q to leave, {escape.describe()} h for help.")
        # Before the tasks, not inside them, so that leaving in the same breath
        # as starting cannot race the decision about what to leave running.
        await start(bar.line)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_show_hub, events, bar, ended, running, tg.cancel_scope)
            tg.start_soon(_send_input, service, hub, receiver, bar, ended)
            tg.start_soon(
                _read_keys, service, hub, console, keys, sender, bar, ended, tg
            )
            tg.start_soon(_tick, bar)

        if running.ours and running.running and not ended.disconnected:
            await _stop_what_we_started(service, hub, bar, running.noun)
        elif running.ours and running.running:
            # Nothing can be sent to a hub that has gone, so whatever it was
            # doing, it is still doing -- and a hub with something running does
            # not advertise. Why it went is not knowable from here: switched
            # off, carried out of range, or its battery flat. Only the first of
            # those needs no rescuing, so the sentence says which case it is
            # about rather than asserting the hub is still on. The gentler fix
            # comes first: the hub's button stops the program, and only a hub
            # that will not answer that needs the power cycle.
            bar.line(
                f"If the hub is still switched on, the {running.noun} is still "
                "running on it, and a hub with something running does not "
                "advertise. Press the hub's button to stop it, or switch the "
                "hub off and on."
            )

    return _ending(ended.disconnected, running)


def _ending(disconnected: bool, running: Running) -> int:
    """Decide what the command should exit with.

    Args:
        disconnected: Whether the hub went away rather than the session ending.
        running: What was on the other end, and how it ended.

    Returns:
        The exit code. Interrupting a program is not a failure of it -- it is
        what the user asked for -- so it is told apart from an exception the
        program did not catch, the way CPython tells them apart.
    """
    if disconnected:
        return EXIT_HUB_ERROR
    if running.interrupted:
        return EXIT_INTERRUPTED
    if running.crashed:
        return EXIT_HUB_ERROR
    return EXIT_OK


async def streamed(
    service: pb_client.Service,
    info: pb_client.HubInfo,
    hub: str | None,
    *,
    replay: int = 0,
    running: Running,
    start: StartFunc,
) -> int:
    """Stay with a program when there is no terminal to do it on.

    What is left of a session when there is nowhere to draw and nothing to type
    at: the program's output as it is printed, and how it ended. This is what a
    command in a pipeline or a build gets, and it is why that output goes to
    stdout -- ``pb run demo.py > log.txt`` should capture what the program
    printed and nothing else.

    Args:
        service: The connected service.
        info: The hub being talked to. Unused, but taken so that this and
            :func:`attached` can stand in for one another.
        hub: Which hub.
        replay: How much earlier output to print first, in bytes.
        running: What the session expects to be talking to.
        start: Starts it, once the output is being listened for.

    Returns:
        The exit code: zero unless the hub went away or the program failed.
    """
    del info
    err = ui.console(err=True)
    out = sys.stdout.buffer
    disconnected = False

    async with service.attach(hub_id=hub, replay_bytes=replay) as events:
        try:
            await start(err.print)
            async for event in events:
                if isinstance(event, pb_client.Output):
                    running.saw(event.data)
                    out.write(event.data)
                    out.flush()
                elif isinstance(event, pb_client.Status):
                    running.note(event)
                    if running.gone:
                        break
                elif not event.connected and event.state == "disconnected":
                    err.print(
                        f"The hub disconnected ({event.reason or 'no reason given'})."
                    )
                    disconnected = True
                    break
        finally:
            # However this ends -- the program finishing, Ctrl-C, a broken pipe
            # -- a program this command started must not be left running on a
            # hub that then cannot be found.
            if running.ours and running.running and not disconnected:
                await _stop_quietly(service, hub, running.noun)

    return _ending(disconnected, running)


async def _stop_quietly(service: pb_client.Service, hub: str | None, noun: str) -> None:
    """Stop what we started, saying nothing about it.

    Args:
        service: The connected service.
        hub: Which hub.
        noun: What is being stopped, for the log.
    """
    # Shielded and bounded, because the likeliest reason to be here is that the
    # command was interrupted, and stopping the program is the whole point.
    with anyio.CancelScope(shield=True), anyio.move_on_after(STOP_SECONDS):
        try:
            await service.stop_program(hub)
        except pb_client.JsonRpcError as exc:
            logger.info("could not stop the %s: %s", noun, exc)


async def _stop_what_we_started(
    service: pb_client.Service,
    hub: str | None,
    bar: banner_module.Banner,
    noun: str,
) -> None:
    """Stop what this session started, on the way out.

    A hub only advertises when nothing is running on it. So a session that
    walks away and leaves something going leaves a hub that nothing can find --
    not the next command, not another machine, not the user wondering why their
    hub has apparently switched itself off. Whoever started it puts it back.

    Something this session merely joined, or a program somebody else started,
    is left alone: it was not ours to stop.

    Args:
        service: The connected service.
        hub: Which hub.
        bar: The status line, for saying what was done and why.
        noun: What to call what was stopped.
    """
    # Shielded and bounded: this runs while the session is already unwinding,
    # and a hub that will not answer must not hold the command open.
    with anyio.move_on_after(STOP_SECONDS, shield=True):
        try:
            await service.stop_program(hub)
        except pb_client.JsonRpcError as exc:
            logger.info("could not stop the %s: %s", noun, exc)
            return
        bar.line(f"{noun.capitalize()} stopped, so the hub can be found again.")
        return
    logger.info("the hub did not confirm the %s had stopped", noun)


class _Ending:
    """How the session is going to stop, shared by the tasks that decide it."""

    def __init__(self) -> None:
        """Start out having ended for no reason yet."""
        self.disconnected = False
        """Set when the hub went away rather than the user leaving."""

        self.flushed = anyio.Event()
        """Set once everything typed has reached the hub, or failed to."""


async def _show_hub(
    events: AsyncIterator[pb_client.HubEvent],
    bar: banner_module.Banner,
    ended: _Ending,
    running: Running,
    scope: anyio.CancelScope,
) -> None:
    """Put everything the hub says on the screen, and notice when it stops.

    Args:
        events: The hub's output, status and state.
        bar: The status line and the screen above it.
        ended: Where to record that the hub went away.
        running: What is running on the hub, kept up to date here.
        scope: The session's scope, cancelled when there is nothing left to
            listen to.
    """
    async for event in events:
        if isinstance(event, pb_client.Output):
            running.saw(event.data)
            bar.write(event.data)
        elif isinstance(event, pb_client.Status):
            running.note(event)
            bar.update(event)
            if running.gone:
                # A program ends by itself, and Ctrl-D ends the interpreter, as
                # does stopping either from anywhere else. However it happened
                # there is nothing left to type at, and a session sitting at a
                # prompt that is not there would be the tool pretending
                # otherwise.
                bar.line(f"The {running.noun} stopped, so the session is over.")
                scope.cancel()
                return
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
    service: pb_client.Service,
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
        service: The connected service.
        hub: Which hub.
        keystrokes: What has been typed and not yet sent.
        bar: The status line, for saying when the hub would not take it.
        ended: Where to report that the queue has run dry.
    """
    async with keystrokes:
        async for data in keystrokes:
            try:
                await service.write_stdin(data, hub_id=hub)
            except pb_client.JsonRpcError as exc:
                logger.info("the hub would not take input: %s", exc)
                bar.notice(str(exc))
    # Nothing more is coming and everything that came has gone, so leaving no
    # longer risks throwing away something that was typed.
    ended.flushed.set()


async def _read_keys(
    service: pb_client.Service,
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
        service: The connected service.
        hub: Which hub.
        console: The terminal.
        keys: The escape filter, which holds what a half-typed command needs.
        keystrokes: Where to put the bytes bound for the hub.
        bar: The status line.
        ended: How the session is ending.
        tg: The session's task group, cancelled when the user leaves.
    """
    try:
        await _keys_until_done(service, hub, console, keys, keystrokes, bar)
    finally:
        # Closed before waiting, so that what is queued is what gets sent and
        # the writer knows nothing more is coming.
        await keystrokes.aclose()

    with anyio.move_on_after(FLUSH_SECONDS):
        await ended.flushed.wait()
    tg.cancel_scope.cancel()


async def _keys_until_done(
    service: pb_client.Service,
    hub: str | None,
    console: console_module.Console,
    keys: escape.Filter,
    keystrokes: MemoryObjectSendStream[bytes],
    bar: banner_module.Banner,
) -> None:
    """Read the keyboard until the user asks to leave.

    Args:
        service: The connected service.
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
            if await _act(action, service, hub, bar):
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
    service: pb_client.Service,
    hub: str | None,
    bar: banner_module.Banner,
) -> bool:
    """Carry out one escape command.

    Args:
        action: What was asked for.
        service: The connected service.
        hub: Which hub.
        bar: The status line and the screen above it.

    Returns:
        True if the session should end.
    """
    if action is Action.QUIT:
        return True
    if action is Action.STOP:
        try:
            await service.stop_program(hub)
        except pb_client.JsonRpcError as exc:
            # A refusal is worth a word in the status line and nothing more:
            # the session is still perfectly usable.
            logger.info("the hub would not stop the program: %s", exc)
            bar.notice(str(exc))
        else:
            bar.notice("stopped")
    elif action is Action.HELP:
        bar.line("")
        for keystrokes, what in escape.help_lines():
            bar.line(f"  {keystrokes:<16} {what}")
        bar.line("")
    else:
        bar.notice(f"no such command; {escape.describe()} h for help")
    return False


async def _tick(bar: banner_module.Banner) -> None:
    """Keep the status line right about the things nothing announces.

    Args:
        bar: The status line.
    """
    while True:
        await anyio.sleep(TICK_SECONDS)
        bar.tick()
