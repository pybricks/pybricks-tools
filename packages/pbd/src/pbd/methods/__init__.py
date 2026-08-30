# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The RPC surface the daemon serves.

One module per namespace -- :mod:`pbd.methods.daemon` for the session itself,
:mod:`pbd.methods.hub` for finding and holding hubs, :mod:`pbd.methods.program`
for getting programs onto them and :mod:`pbd.methods.stdio` for talking to what
is running. :mod:`pbd.methods.registry` holds the table they all register into.

Importing a module is what registers its methods, so :mod:`pbd.cli` imports all
four and nothing else has to keep a list of them in step.
"""

from __future__ import annotations

from pbd.methods.registry import METHODS, Method, MethodFunc, method, require

__all__ = ["METHODS", "Method", "MethodFunc", "method", "require"]
