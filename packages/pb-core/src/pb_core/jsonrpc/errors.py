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

    # Firmware.
    FIRMWARE_NOT_FOUND = -32040
    """No firmware matches what was asked for.

    A version that was never published, or a hub type with no image -- there
    is no official firmware to restore a Move, City or Technic hub to, for
    instance.
    """

    FIRMWARE_UNAVAILABLE = -32041
    """The image is not cached and could not be fetched.

    Separate from not existing, because the remedies are different: one is a
    typo and the other is a network, a rate limit, or a machine deliberately
    kept offline.
    """

    FIRMWARE_BAD_ARCHIVE = -32042
    """The archive could not be read as firmware.

    A truncated download, something that is not a firmware zip at all, or
    metadata this build does not understand.
    """

    FIRMWARE_INCOMPATIBLE = -32043
    """The image does not match the hub in front of us.

    Wrong hub type, or a Prime archive with no variant for the silicon this
    particular hub turned out to have.
    """

    FIRMWARE_UPDATE_MODE_FAILED = -32044
    """The hub did not appear in its bootloader.

    Which usually means it switched off instead of rebooting, and always means
    the user has to put it into update mode by hand.
    """

    FIRMWARE_LICENSE_NOT_ACCEPTED = -32045
    """Nobody has accepted the licence for this image yet."""

    FIRMWARE_HUB_NAME_TOO_LONG = -32046
    """The requested hub name does not fit the field the firmware reserves."""

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
