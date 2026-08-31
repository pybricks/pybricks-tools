# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The command line's shape: the app itself, its groups, and shared options.

Separate from the commands so that the command modules can all import it
without importing each other. :mod:`pb_cli.cli` pulls those modules in, which is
what registers them.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from typing import Annotated

import cyclopts

from pb_cli import __version__
from pb_cli.config import config_sources
from pb_core.logging import TRACE, setup_logging

__all__ = [
    "FIRMWARE_COMMANDS",
    "HUB_COMMANDS",
    "PROGRAM_COMMANDS",
    "SERVICE_COMMANDS",
    "Verbose",
    "app",
    "configure_logging",
    "firmware_app",
    "service_app",
]

app = cyclopts.App(
    name="pb",
    version=__version__,
    help=(
        "Talk to Pybricks hubs.\n\n"
        "A hub stays connected between commands, so you only need to run "
        "pb connect once."
    ),
    # Every command is in a group of its own, so what is left in the default
    # panel is --help and --version. Calling that panel "Commands" would be a
    # lie, and putting it last keeps the commands at the top where someone
    # reading the help is looking.
    group_commands=cyclopts.Group("Options", sort_key=4),
    config=config_sources(),
)

HUB_COMMANDS = cyclopts.Group("Hubs", sort_key=0)
"""Finding a hub, holding on to one, and asking what it is doing.

Ordered by what someone actually has to do, which is why each command carries
an explicit ``sort_key`` rather than falling back to alphabetical. ``connect``
comes first, because it is the one command nobody gets anywhere without, with
its opposite ``disconnect`` beside it.

The rest are things a user may want rather than steps they must take, so they
come after: ``status`` and ``list`` report on what is already connected, and
``scan`` comes last because nobody has to run it at all -- ``connect`` does its
own looking, and ``connect --scan`` shows the same list when the choice
matters.
"""

PROGRAM_COMMANDS = cyclopts.Group("Programs", sort_key=1)
"""Getting a program onto a hub and controlling it once it is there."""

FIRMWARE_COMMANDS = cyclopts.Group("Firmware", sort_key=2)
"""Installing Pybricks on a hub, and the images that get installed.

After programs because it is the rarer job: most days nobody installs
firmware. Before the service because it is something a user does deliberately,
where the service is something they should be able to forget about.
"""

SERVICE_COMMANDS = cyclopts.Group("Background service", sort_key=3)
"""The thing holding the connections open. Rarely needed, so it comes last."""

service_app = cyclopts.App(
    name="service",
    help="Manage the background service that keeps hubs connected.",
    group=SERVICE_COMMANDS,
    # Not inherited from the root app, where the default panel holds only the
    # flags: down here it really is the list of commands.
    group_commands=cyclopts.Group("Commands"),
)
app.command(service_app)

firmware_app = cyclopts.App(
    name="firmware",
    help="Install Pybricks on a hub, and manage the images it installs.",
    group=FIRMWARE_COMMANDS,
    group_commands=cyclopts.Group("Commands"),
)
app.command(firmware_app)


Verbose = Annotated[
    Sequence[bool],
    cyclopts.Parameter(
        name=["-v", "--verbose"],
        help="Increase log detail. Repeat for more (-v, -vv, -vvv).",
        show_default=False,
        # No --no-verbose: there is nothing to turn off, and offering it
        # would be one more thing in every command's help for no reason.
        negative=(),
    ),
]
"""How loud to be, counted rather than measured.

A sequence of flags rather than a number, because that is what makes the help
true. As an ``int`` this took a value -- ``-v 1`` worked and ``-v`` was an
error saying it "requires an argument", while every command's help said to
repeat it. Collecting the flags gives ``-v``, ``-v -v`` and the stacked
``-vv``, which is what anybody typing it expects.
"""


def configure_logging(verbose: Verbose | int = ()) -> None:
    """Map repeated ``-v`` flags onto log levels.

    Args:
        verbose: The flags as they were given, or how many there
            were. Both, because the commands hand over what they
            were passed and a count reads better everywhere else.
    """
    count = verbose if isinstance(verbose, int) else len(verbose)
    if count <= 0:
        level = logging.WARNING
    elif count == 1:
        level = logging.INFO
    elif count == 2:
        level = logging.DEBUG
    else:
        level = TRACE
    setup_logging(
        level=level,
        style="rich" if sys.stderr.isatty() else "plain",
        # bleak is noisy enough to bury our own lines, and is only interesting
        # once someone is debugging the radio itself.
        quiet_loggers=("bleak", "asyncio") if count < 3 else (),
    )
