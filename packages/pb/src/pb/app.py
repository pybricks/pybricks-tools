# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The command line's shape: the app itself, its groups, and shared options.

Separate from the commands so that the command modules can all import it
without importing each other. :mod:`pb.cli` pulls those modules in, which is
what registers them.
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated

import cyclopts

from pb import __version__
from pb.config import config_sources
from pb_core.logging import TRACE, setup_logging

__all__ = [
    "HUB_COMMANDS",
    "PROGRAM_COMMANDS",
    "SERVICE_COMMANDS",
    "Verbose",
    "app",
    "configure_logging",
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
    group_commands=cyclopts.Group("Options", sort_key=3),
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

SERVICE_COMMANDS = cyclopts.Group("Background service", sort_key=2)
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


Verbose = Annotated[
    int,
    cyclopts.Parameter(
        name=["-v", "--verbose"],
        help="Increase log detail. Repeat for more (-v, -vv, -vvv).",
        show_default=False,
    ),
]


def configure_logging(verbose: int) -> None:
    """Map repeated ``-v`` flags onto log levels.

    Args:
        verbose: How many times ``-v`` was given.
    """
    if verbose <= 0:
        level = logging.WARNING
    elif verbose == 1:
        level = logging.INFO
    elif verbose == 2:
        level = logging.DEBUG
    else:
        level = TRACE
    setup_logging(
        level=level,
        style="rich" if sys.stderr.isatty() else "plain",
        # bleak is noisy enough to bury our own lines, and is only interesting
        # once someone is debugging the radio itself.
        quiet_loggers=("bleak", "asyncio") if verbose < 3 else (),
    )
