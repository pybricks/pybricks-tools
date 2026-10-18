# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Finding hubs, holding on to them, and asking what they are doing.

Each command is a thin adapter: it reads the arguments, hands the work to an
async body, and lets :mod:`pb_cli.errors` present whatever goes wrong. No command
catches its own failures, because a task group would have wrapped them in an
``ExceptionGroup`` anyway -- the exceptions being the few places where failing
is a perfectly good answer to the question asked, such as listing hubs when
nothing is connected.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Annotated, Final

import anyio
import cyclopts
from rich.console import Console, RenderableType

import pb_client
from pb_cli import errors, keys, picker, ui
from pb_cli.app import HUB_COMMANDS, Verbose, app, configure_logging
from pb_cli.errors import EXIT_OK, PERMISSION_NAME
from pb_core.jsonrpc import ErrorCode

logger = logging.getLogger(__name__)

__all__ = [
    "connect_cmd",
    "disconnect_cmd",
    "list_cmd",
    "scan_cmd",
    "status_cmd",
    "use_cmd",
]

_ID_PREFIXES = ("ble:", "fake:", "usb:", "rfcomm:")

_HANDLED_HERE = (pb_client.PbClientError, BaseExceptionGroup)
"""What to catch when a command deals with a failure itself.

Catching the error's own type is not enough: anything raised inside an
``async with pb_client.connect()`` block comes back wrapped in a group of one,
so the type to catch is not the type that was raised.
"""

_NOT_FOUND_HINTS: Final[Mapping[str | None, str]] = {
    None: (
        "Switch the hub on and press its button so that its light flashes. "
        "If it is new, is the Pybricks firmware installed?"
    ),
    "usb": (
        "Switch the hub on and plug it in. Not every hub has a USB port: the "
        "City, Technic and Move hubs are Bluetooth only."
    ),
    "ble": (
        "Switch the hub on and press its button so that its light flashes. A "
        "hub that is already connected, or running a program, does not "
        "advertise."
    ),
    "rfcomm": (
        "Pair the hub with this computer in its Bluetooth settings first. Only "
        "the EV3 and NXT are reached this way."
    ),
}
"""What to suggest when a hub to connect to could not be found.

Keyed by the transport that was asked for, because the advice is different and
the wrong half of it is worse than none: telling somebody to watch for a
flashing light when they asked for a cable sends them to do something that
cannot help, and this tool's rule is that a suggestion has to be one.

Everywhere else, a missing hub means one that was expected to be connected
already, and the suggestion is to run [bold]pb connect[/] -- which would be
silly advice for [bold]pb connect[/] itself.
"""


def _connect_hints(transport: str | None) -> Mapping[int, str]:
    """Build the not-found advice for the transport that was asked for.

    Args:
        transport: What ``--transport`` said, or ``None`` for any.

    Returns:
        The hints to hand to the error renderer.
    """
    hint = _NOT_FOUND_HINTS.get(transport, _NOT_FOUND_HINTS[None])

    return {ErrorCode.HUB_NOT_FOUND: hint}


def _asked(platform: str = sys.platform) -> str:
    """Say that the system is asking whether the service may use Bluetooth.

    Args:
        platform: What to say it for. Only macOS asks, today.

    Returns:
        The sentence, with markup.
    """
    who = "macOS" if platform == "darwin" else "The system"
    return (
        f"{who} is asking whether [bold]{PERMISSION_NAME}[/] may use "
        "Bluetooth. Answer the dialog to carry on."
    )


@asynccontextmanager
async def _telling_when_asked(
    service: pb_client.Service, say: Callable[[bool], None]
) -> AsyncGenerator[None, None]:
    """Say when the system starts or stops asking for Bluetooth, while hubs are sought.

    Open it before the scan or connection it is for, so that nothing it reports
    is missed. The search itself carries on once the user answers, so this only
    has to tell them to; what changes is whether they look for the dialog now
    or are told to answer it once the search has given up.

    Args:
        service: Where the search runs.
        say: Called with True when the system starts asking, and with False if
            it stops before the search ends.

    Yields:
        Nothing. The block is the search.
    """

    async def follow(
        changes: AsyncIterator[tuple[pb_client.UnusableAdapter, ...]],
    ) -> None:
        asked = False

        async for unusable in changes:
            now = any(adapter.awaiting_permission for adapter in unusable)

            if now != asked:
                asked = now
                say(asked)

    async with service.watching_adapters() as changes, anyio.create_task_group() as tg:
        tg.start_soon(follow, changes)

        try:
            yield
        finally:
            tg.cancel_scope.cancel()


def _looks_like_id(value: str) -> bool:
    """Whether a string is a hub id rather than a hub name."""
    return value.startswith(_ID_PREFIXES)


@app.command(name="scan", group=HUB_COMMANDS, sort_key=5)
async def scan_cmd(
    *,
    # ASYNC109 catches a timeout threaded into an async API, and this is the
    # exception it cannot know about: the name is the spelling of a
    # command-line option, not a deadline. It says how long the radio should
    # listen, which is a real parameter of scanning, and reaches pb-client as
    # `seconds`. A caller wanting to bound its own wait still opens a cancel
    # scope, as the house rules require.
    timeout: float = 10.0,  # noqa: ASYNC109
    name: str | None = None,
    transport: str | None = None,
    verbose: Verbose = (),
) -> int:
    """Look for Pybricks hubs that are switched on and in range.

    Nothing is connected to, which is the point: this answers "is it on, and
    can I hear it?" without taking the hub away from whatever else might want
    it.

    Args:
        timeout: How long to scan, in seconds.
        name: Only show hubs with this name.
        transport: Only look on this transport: usb, ble or rfcomm. All of
            them by default.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_scan, timeout, name, transport)


async def _scan(seconds: float, name: str | None, transport: str | None) -> int:
    out, err = ui.console(), ui.console(err=True)
    seen: dict[str, pb_client.DeviceInfo] = {}
    caption = f"Scanning for {seconds:.0f}s..."

    def scanning(
        service: pb_client.Service,
    ) -> AbstractAsyncContextManager[AsyncIterator[pb_client.DeviceInfo]]:
        return service.scanning(seconds=seconds, name=name, transport=transport)

    async with pb_client.connect() as service:
        if out.is_terminal:
            # Redrawn in place as advertisements arrive, so a hub appears the
            # moment it is heard and its signal moves as it is carried about.
            with ui.live(ui.scan_table((), caption), out) as display:

                def say(asked: bool) -> None:
                    # In the caption, not printed: this display is on stdout,
                    # and a line on stderr would land in the middle of it.
                    nonlocal caption
                    caption = _asked() if asked else f"Scanning for {seconds:.0f}s..."
                    display.update(ui.scan_table(seen.values(), caption), refresh=True)

                async with (
                    _telling_when_asked(service, say),
                    scanning(service) as devices,
                ):
                    async for device in devices:
                        seen[device.id] = device
                        display.update(
                            ui.scan_table(seen.values(), caption), refresh=True
                        )

                display.update(ui.scan_table(seen.values(), f"Found {len(seen)}."))
        else:

            def tell(asked: bool) -> None:
                if asked:
                    err.print(_asked())

            async with _telling_when_asked(service, tell), scanning(service) as devices:
                # Piped: one line per hub, printed the moment it is first
                # heard, so that "pb scan | head -1" answers as soon as there
                # is an answer.
                async for device in devices:
                    if device.id not in seen:
                        line = (
                            f"{device.name}\t{device.id}"
                            if ui.SHOW_HUB_IDS
                            else device.name
                        )
                        ui.plain(out, line)

                    seen[device.id] = device

    if not seen:
        if transport == "rfcomm":
            # The listing is the paired set, so an empty one means nothing is
            # paired. Switching the hub on would not change it.
            err.print("[yellow]No hubs found.[/] Is the hub paired with this computer?")
        else:
            # "In range" is Bluetooth's word for it and means nothing to
            # somebody who asked about a cable.
            where = "plugged in" if transport == "usb" else "in range"
            err.print(f"[yellow]No hubs found.[/] Is the hub switched on and {where}?")

    return EXIT_OK


@app.command(name="connect", group=HUB_COMMANDS, sort_key=0)
async def connect_cmd(
    *,
    hub: str | None = None,
    scan: Annotated[bool, cyclopts.Parameter(name=["-s", "--scan"])] = False,
    transport: str | None = None,
    verbose: Verbose = (),
) -> int:
    """Connect to a hub and keep it connected.

    The connection outlives this command, so later commands reuse it rather
    than reconnecting.

    Args:
        hub: The hub's name, as shown by pb scan. Defaults to the first
            hub found.
        scan: Watch for hubs and choose one from the list. There is no time
            limit, so the hub can be switched on whenever you like, and
            Ctrl-C gives up without connecting to anything.
        transport: Reach the hub this way: usb, ble or rfcomm. By default
            whichever works, preferring USB where a hub is reachable more than
            one way.
        verbose: Increase log detail.
    """

    if scan:
        # --scan wins over a named hub rather than being refused
        # alongside one. The name may have come from the environment or
        # from the project's pyproject.toml, and refusing a value the
        # user never typed would make --scan unusable inside a project.
        hub = None

    if scan and not keys.keys_available():
        # Better to say so than to sit waiting for a keystroke that cannot
        # arrive, which is what a piped or emulated terminal would do.
        return ui.usage(
            "Choosing a hub needs a terminal to read the arrow keys from.",
            "Name the hub with [bold]--hub[/] instead, or run "
            "[bold]pb scan[/] to see what is nearby.",
        )

    configure_logging(verbose)
    return await errors.run(
        _connect, hub, scan, transport, hints=_connect_hints(transport)
    )


async def _connect(hub: str | None, scan: bool, transport: str | None) -> int:
    err = ui.console(err=True)

    def tell(asked: bool) -> None:
        # Printed above the spinner or the chooser, both of which are drawn
        # on this console and so make room for it.
        if asked:
            err.print(_asked())

    async with pb_client.connect() as service:
        if scan:
            # No time limit: the hub may still be in the next room. The scan
            # ends when the block does, whether the user chose or gave up.
            async with (
                _telling_when_asked(service, tell),
                service.scanning(seconds=None, transport=transport) as devices,
            ):
                device = await picker.choose_hub(err, devices, ui.chooser_table)

            if device is None:
                # Deciding not to connect is what was asked for, not a failure.
                err.print("No hub chosen.")
                return EXIT_OK

            with ui.spinner(err, "Connecting..."):
                info = await service.connect_hub(hub_id=device.id)
                # No transport here: choosing a row from the list has already
                # chosen the route, since each row is one.
        else:
            with ui.spinner(err, "Connecting..."):
                async with _telling_when_asked(service, tell):
                    info = await service.connect_hub(
                        hub_id=hub if hub and _looks_like_id(hub) else None,
                        name=hub if hub and not _looks_like_id(hub) else None,
                        transport=transport,
                    )

    # The protocol version belongs to pb status: it explains why a command was
    # refused, but on a successful connection it is one more number to read
    # past.
    described = f"firmware {info.firmware}"

    if info.hub_type:
        described = f"{info.hub_type}, {described}"

    err.print(f"Connected to [bold]{info.name}[/] ({described}).")
    return EXIT_OK


@app.command(name="disconnect", group=HUB_COMMANDS, sort_key=1)
async def disconnect_cmd(*, hub: str | None = None, verbose: Verbose = ()) -> int:
    """Disconnect a hub.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_disconnect, hub)


async def _disconnect(hub: str | None) -> int:
    err = ui.console(err=True)

    try:
        async with pb_client.connect(autostart=False) as service:
            await service.disconnect_hub(hub)
    except _HANDLED_HERE as exc:
        # Nothing connected means there is nothing to disconnect, which is the
        # outcome the user asked for rather than a failure.
        if errors.find(exc, pb_client.ServiceUnavailable) is None:
            raise

        err.print("No hub is connected.")
        return EXIT_OK

    err.print("Disconnected.")
    return EXIT_OK


@app.command(name="list", group=HUB_COMMANDS, sort_key=3)
async def list_cmd(*, verbose: Verbose = ()) -> int:
    """List the hubs that are currently connected.

    Args:
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_list)


async def _list() -> int:
    out, err = ui.console(), ui.console(err=True)

    try:
        async with pb_client.connect(autostart=False) as service:
            hubs, acting = await service.list_hubs()
    except _HANDLED_HERE as exc:
        # An empty list is a real answer to "what is connected?".
        if errors.find(exc, pb_client.ServiceUnavailable) is None:
            raise

        err.print("No hubs are connected.")
        return EXIT_OK

    if not hubs:
        err.print("No hubs connected. Use [bold]pb connect[/].")
        return EXIT_OK

    if out.is_terminal:
        out.print(ui.connected_table(hubs, acting))
    else:
        for info in hubs:
            marker = "*" if info.id == acting else "-"
            fields = [marker, info.name, info.id, info.firmware]

            if not ui.SHOW_HUB_IDS:
                del fields[2]

            ui.plain(out, "\t".join(fields))

    return EXIT_OK


@app.command(name="use", group=HUB_COMMANDS, sort_key=4)
async def use_cmd(
    *,
    hub: str | None = None,
    clear: bool = False,
    verbose: Verbose = (),
) -> int:
    """Choose the hub that commands act on when none is named.

    With no options, this lists the hubs that are connected and the hubs it
    can hear, and lets you choose one with the arrow keys.

    What is remembered is the hub's name, so the choice survives the hub being
    switched off and on, and outlives the background service. Every shell sees
    the same choice.

    Args:
        hub: The hub's name. It does not have to be connected, or even switched
            on, so the hub a project belongs to can be chosen before any of it
            is plugged in.
        clear: Forget the chosen hub. Commands then act on whichever hub is
            connected, as long as there is only one.
        verbose: Increase log detail.
    """

    if clear:
        # --clear wins over a named hub, for the same reason --scan does
        # on connect: the name may have come from a project file rather
        # than from the person typing, and forgetting the chosen hub
        # would otherwise be impossible in a project directory.
        hub = None

    configure_logging(verbose)
    return await errors.run(_use, hub, clear)


async def _use(hub: str | None, clear: bool) -> int:
    err = ui.console(err=True)

    async with pb_client.connect() as service:
        if clear:
            await service.use_hub(None)
            err.print(
                "No hub is chosen. Commands will use the connected hub, "
                "while there is only one."
            )
            return EXIT_OK

        if hub is None:
            if not keys.keys_available():
                # Nothing can be chosen without a terminal to choose in, so
                # answer the other question this command can answer: which hub
                # is chosen already.
                return await _say_which(service, err)

            hub = await _choose(service, err)
            if hub is None:
                err.print("Nothing chosen.")
                return EXIT_OK

        choice = await service.use_hub(hub)
        err.print(f"Now using [bold]{choice.name}[/].")

        if not choice.connected:
            # Worth saying, because nothing else will: commands go on using
            # the hub that is connected until this one is.
            err.print("It is not connected at the moment.")

    return EXIT_OK


async def _say_which(service: pb_client.Service, err: Console) -> int:
    """Report the chosen hub, for a caller with no terminal to choose in.

    Args:
        service: The connected service.
        err: Where messages go.

    Returns:
        The exit code. Nothing chosen is an answer rather than a failure, so
        it is still zero; the name goes to stdout, where a script reading it
        gets the name and nothing else.
    """
    choice = await service.chosen_hub()
    if choice.name is None:
        err.print("No hub is chosen.")
        return EXIT_OK

    ui.plain(ui.console(), choice.name)
    return EXIT_OK


async def _choose(service: pb_client.Service, err: Console) -> str | None:
    """Let the user pick from the hubs that are connected or in range.

    Args:
        service: The connected service.
        err: Where the list is drawn.

    Returns:
        The chosen hub's name, or None if the user left without choosing.
    """
    hubs, _ = await service.list_hubs()
    connected = [
        pb_client.DeviceInfo(
            id=info.id,
            name=info.name,
            address=info.address,
            rssi=None,
            transport=info.id.split(":", 1)[0],
            hub_type=info.hub_type,
        )
        for info in hubs
    ]
    known = {device.id for device in connected}

    async def offered() -> AsyncIterator[pb_client.DeviceInfo]:
        """Offer the connected hubs first, then whatever the radio hears."""

        for device in connected:
            yield device

        async for device in found:
            # A connected hub does not advertise, so this is belt and braces:
            # what it prevents is a hub arriving twice under two descriptions.
            if device.id not in known:
                yield device

    def render(devices: list[pb_client.DeviceInfo], selected: int) -> RenderableType:
        return ui.chooser_table(devices, selected, connected=known)

    # No time limit: the hub may still be in the next room.
    async with service.scanning(seconds=None) as found:
        device = await picker.choose_hub(err, offered(), render, verb="use it")

    return device.name if device is not None else None


@app.command(name="status", group=HUB_COMMANDS, sort_key=2)
async def status_cmd(
    *, hub: str | None = None, full: bool = False, verbose: Verbose = ()
) -> int:
    """Show what a hub is doing.

    Args:
        hub: Which hub, by name. Defaults to the one in use.
        full: Show every detail, including the ones that only mean something
            when you are debugging.
        verbose: Increase log detail.
    """
    configure_logging(verbose)
    return await errors.run(_status, hub, full)


async def _status(hub: str | None, full: bool) -> int:
    out = ui.console()

    async with pb_client.connect(autostart=False) as service:
        info = await service.hub_info(hub)

    rows = [("name", info.name)]

    if ui.SHOW_HUB_IDS:
        rows.append(("id", info.id))

    rows.append(("state", info.state))

    # Which way the hub is reached, because it decides how quick a command is
    # and, over USB, whether the hub is still advertising for anything else to
    # find. Left out entirely when the service did not say, rather than shown
    # blank.
    connection = ui.transport_name(info.transport)
    if connection:
        rows.append(("connection", connection))

    if info.hub_type:
        rows.append(("type", info.hub_type))

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
    warnings = ui.hub_warnings(info.status_names)
    if warnings:
        rows.append(("warnings", ", ".join(warnings)))

    if full and info.status_names:
        rows.append(("flags", ", ".join(info.status_names)))

    ui.rows(out, rows)
    return EXIT_OK
