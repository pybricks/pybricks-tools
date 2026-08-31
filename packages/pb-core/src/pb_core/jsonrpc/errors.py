# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""JSON-RPC error codes.

The standard codes come from the specification. Everything specific to this
project lives in the -32000..-32099 range, which the specification reserves for
implementation-defined server errors.

Hub failures keep the raw ``pbio_pybricks_error_t`` value in ``data`` so the CLI
can report exactly what the firmware said rather than a paraphrase.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

__all__ = ["ErrorCode", "JsonRpcError", "ProtocolError"]


class ErrorCode(IntEnum):
    """Codes carried in a JSON-RPC error response."""

    # Standard, from the JSON-RPC 2.0 specification.
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # Session and service.
    SERVICE_SHUTTING_DOWN = -32000
    HELLO_REQUIRED = -32001
    UNSUPPORTED_PROTOCOL_VERSION = -32002
    NOT_AUTHENTICATED = -32003

    # Hubs.
    HUB_NOT_FOUND = -32010
    HUB_NOT_CONNECTED = -32011
    HUB_ALREADY_CONNECTED = -32012
    HUB_DISCONNECTED = -32013
    HUB_BUSY = -32014
    HUB_INVALID_COMMAND = -32015
    HUB_UNSUPPORTED = -32016

    # Programs.
    PROGRAM_TOO_LARGE = -32017
    COMPILE_FAILED = -32018
    POWER_BUTTON_PRESSED = -32019
    PROGRAM_SLOT_NOT_SELECTED = -32022
    """A download was asked for a slot other than the one the hub has chosen.

    The hub decides where a program lands, and nothing on the wire can change
    that, so naming a different slot is a request that cannot be carried out.
    """

    # Transport and lifecycle.
    BLUETOOTH_UNAVAILABLE = -32020
    SCAN_TIMEOUT = -32021
    REQUEST_CANCELLED = -32030


class ProtocolError(Exception):
    """A message could not be parsed or violates the protocol.

    Raised locally while decoding. It is not itself a wire error, though the
    peer usually answers with :attr:`ErrorCode.PARSE_ERROR` afterwards.
    """


class JsonRpcError(Exception):
    """An error response received from, or to be sent to, the peer."""

    def __init__(
        self,
        code: ErrorCode | int,
        message: str,
        data: Any = None,
    ) -> None:
        """Build an error.

        Args:
            code: A member of :class:`ErrorCode`, or a raw integer.
            message: Short human-readable description.
            data: Structured detail, such as the hub id and the raw
                ``pbio_pybricks_error_t`` value.
        """
        super().__init__(message)
        self.code = int(code)
        self.message = message
        self.data = data

    def __str__(self) -> str:
        """Render the error with its numeric code."""
        return f"[{self.code}] {self.message}"
