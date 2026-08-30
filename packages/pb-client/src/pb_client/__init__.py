# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Client library for the Pybricks daemon.

The public surface is deliberately shaped so that structured concurrency cannot
be violated from outside: anything producing ongoing events is an async context
manager yielding an async iterator, and no function takes a ``timeout``
argument. Callers scope cancellation with :func:`anyio.fail_after` or
:func:`anyio.move_on_after` instead.
"""

__version__ = "0.1.0"

from pb_client.compile import (
    SOURCE_NAME,
    CompileError,
    compile_script,
    compile_source,
    describe_compiler,
)
from pb_client.daemon import Daemon, DaemonInfo, DeviceInfo, HubInfo, connect
from pb_client.errors import (
    DaemonStartError,
    DaemonUnavailable,
    PbClientError,
)
from pb_core.jsonrpc import JsonRpcError

__all__ = [
    "SOURCE_NAME",
    "CompileError",
    "Daemon",
    "DaemonInfo",
    "DaemonStartError",
    "DaemonUnavailable",
    "DeviceInfo",
    "HubInfo",
    "JsonRpcError",
    "PbClientError",
    "__version__",
    "compile_script",
    "compile_source",
    "connect",
    "describe_compiler",
]
