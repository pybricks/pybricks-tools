# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The RPC surface the service serves.

One module per namespace -- :mod:`pb_service.methods.service` for the session itself,
:mod:`pb_service.methods.hub` for finding and holding hubs, :mod:`pb_service.methods.program`
for getting programs onto them and :mod:`pb_service.methods.stdio` for talking to what
is running. :mod:`pb_service.methods.registry` holds the table they all register into.

Importing a module is what registers its methods, so :mod:`pb_service.cli` imports all
four and nothing else has to keep a list of them in step.
"""

from __future__ import annotations

from pb_service.methods.registry import METHODS, Method, MethodFunc, method, require

__all__ = ["METHODS", "Method", "MethodFunc", "method", "require"]
