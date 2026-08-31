# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Client library for the Pybricks service.

The public surface is deliberately shaped so that structured concurrency cannot
be violated from outside: anything producing ongoing events is an async context
manager yielding an async iterator, and no function takes a ``timeout``
argument. Callers scope cancellation with
[fail_after][anyio.fail_after] or
[move_on_after][anyio.move_on_after] instead.
"""

__version__ = "0.1.0"

from pb_client.compile import (
    SOURCE_NAME,
    CompileError,
    compile_script,
    compile_source,
    describe_compiler,
)
from pb_client.errors import (
    PbClientError,
    ServiceStartError,
    ServiceTooOld,
    ServiceUnavailable,
)
from pb_client.firmware import (
    CachedFirmware,
    Fetched,
    FirmwareCatalogue,
    FirmwareEvent,
    FirmwareRelease,
    Progress,
    Removed,
    Step,
)
from pb_client.models import (
    Choice,
    DeviceInfo,
    Downloaded,
    HubInfo,
    ServiceInfo,
)
from pb_client.service import Service, connect
from pb_client.stdio import REPL_PROGRAM_ID, HubEvent, Output, State, Status
from pb_core.jsonrpc import JsonRpcError

__all__ = [
    "REPL_PROGRAM_ID",
    "SOURCE_NAME",
    "CachedFirmware",
    "Choice",
    "CompileError",
    "DeviceInfo",
    "Downloaded",
    "Fetched",
    "FirmwareCatalogue",
    "FirmwareEvent",
    "FirmwareRelease",
    "HubEvent",
    "HubInfo",
    "JsonRpcError",
    "Output",
    "PbClientError",
    "Progress",
    "Removed",
    "Service",
    "ServiceInfo",
    "ServiceStartError",
    "ServiceTooOld",
    "ServiceUnavailable",
    "State",
    "Status",
    "Step",
    "__version__",
    "compile_script",
    "compile_source",
    "connect",
    "describe_compiler",
]
