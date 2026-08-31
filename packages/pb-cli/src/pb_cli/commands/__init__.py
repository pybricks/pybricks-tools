# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The commands ``pb`` offers, grouped the way its help groups them.

One module per group -- :mod:`pb_cli.commands.hub` for finding and holding a hub,
:mod:`pb_cli.commands.program` for getting programs onto it, :mod:`pb_cli.commands.repl`
for talking to it directly, and :mod:`pb_cli.commands.service` for the thing that
keeps the connection open.

Importing a module is what registers its commands with the application in
:mod:`pb_cli.app`, so :mod:`pb_cli.cli` imports them all and there is no second list of
commands to keep in step with this one.
"""

from __future__ import annotations
