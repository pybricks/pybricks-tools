# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The commands ``pb`` offers, grouped the way its help groups them.

One module per group -- :mod:`pb.commands.hub` for finding and holding a hub,
:mod:`pb.commands.program` for getting programs onto it, :mod:`pb.commands.repl`
for talking to it directly, and :mod:`pb.commands.service` for the thing that
keeps the connection open.

Importing a module is what registers its commands with the application in
:mod:`pb.app`, so :mod:`pb.cli` imports them all and there is no second list of
commands to keep in step with this one.
"""

from __future__ import annotations
