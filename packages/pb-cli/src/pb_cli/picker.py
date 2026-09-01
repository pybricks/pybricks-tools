# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Choosing a hub from the ones the radio can hear.

The list is being written while it is being read: hubs appear as they are
heard, and a hub already listed changes as its signal does. So the display is
redrawn on both counts, and the selection follows the hub it is on rather than
the row number, or switching a second hub on would move the highlight off the
one the user had settled on.

There is no time limit. The person may be fetching the hub from another room,
and a deadline on a person only ever expires at the worst moment. Leaving
without choosing is an ordinary outcome rather than an error, so Escape, ``q``
and Ctrl-C all simply return nothing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable

import anyio
from rich.console import Console, Group, RenderableType
from rich.text import Text

import pb_client
from pb_cli import keys, ui
from pb_cli.keys import Key

logger = logging.getLogger(__name__)

__all__ = ["choose_hub"]

# The line under the list. What Enter does depends on who is asking --
# connecting to a hub and choosing which one to work with are different
# answers -- so the verb is the caller's to supply. A comment rather than a
# docstring: a documented attribute is rendered into the API reference, and
# the arrows in it cannot be encoded by the formatter the renderer shells out
# to on a Windows machine with a legacy code page.
HELP = "↑↓ to choose, Enter to {verb}, Ctrl-C to stop looking"


async def choose_hub(
    err: Console,
    devices: AsyncIterator[pb_client.DeviceInfo],
    render: Callable[[list[pb_client.DeviceInfo], int], RenderableType],
    verb: str = "connect",
) -> pb_client.DeviceInfo | None:
    """Show hubs as they are found and let the user pick one.

    Args:
        err: The console to draw on. Not stdout: a chooser is not data, and
            the hub that comes out of it may well be piped somewhere.
        devices: Hubs as they are heard, one item per advertisement.
        render: Draws the list, given the hubs in the order first seen and the
            index of the selected one. Passed in so that this module does not
            have to know what a hub looks like on screen.
        verb: What pressing Enter will do, for the help line.

    Returns:
        The hub the user chose, or ``None`` if they left without choosing.
    """
    seen: dict[str, pb_client.DeviceInfo] = {}
    order: list[str] = []
    selected = 0
    chosen: pb_client.DeviceInfo | None = None

    def frame() -> RenderableType:
        listed = [seen[device_id] for device_id in order]
        return Group(
            render(listed, selected),
            Text(HELP.format(verb=verb), style="dim"),
        )

    with keys.reading_keys(), ui.live(frame(), err) as live:
        async with anyio.create_task_group() as tg:

            async def watch() -> None:
                async for device in devices:
                    if device.id not in seen:
                        order.append(device.id)

                    seen[device.id] = device
                    live.update(frame(), refresh=True)

            tg.start_soon(watch)

            try:
                while True:
                    key = keys.poll_key()
                    if key is Key.CANCEL:
                        break

                    if key is Key.ENTER and order:
                        chosen = seen[order[selected]]
                        break

                    if key in (Key.UP, Key.DOWN) and order:
                        step = -1 if key is Key.UP else 1
                        # Wrapping, because a list of two with the highlight
                        # stuck at one end is a worse surprise than either
                        # choice here.
                        selected = (selected + step) % len(order)
                        live.update(frame(), refresh=True)
                    elif key is None:
                        # Nothing typed, so wait for something to be. Waiting
                        # rather than sleeping costs nothing here and is what
                        # the house rule asks for; the list goes on filling
                        # in either way, because the task above redraws it as
                        # each hub is heard rather than on a timer.
                        await keys.wait_for_key()
            except KeyboardInterrupt:
                # Caught here rather than left to the top level, because the
                # task group would wrap it and because giving up on finding a
                # hub is not a failure. Caught inside the group so that it is
                # never wrapped in the first place.
                logger.debug("nothing chosen: interrupted")
            finally:
                # The scan belongs to the caller, but nothing started here
                # should outlive the choosing.
                tg.cancel_scope.cancel()

    return chosen
