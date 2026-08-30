# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""JSON-RPC 2.0 over the IPC transport.

Newline-delimited JSON was chosen so that the protocol stays inspectable and
usable from other languages: a future editor extension, or one of the
app-data service clients, should not need to link against this code.
"""

from __future__ import annotations

from pb_core.jsonrpc.errors import ErrorCode, JsonRpcError, ProtocolError
from pb_core.jsonrpc.framing import MAX_LINE_BYTES, LineFramer
from pb_core.jsonrpc.messages import (
    CANCEL_METHOD,
    JSONRPC_VERSION,
    ErrorObject,
    Message,
    Notification,
    Request,
    RequestId,
    Response,
    decode_binary,
    encode_binary,
    error_response,
    parse_message,
    to_wire,
)

__all__ = [
    "CANCEL_METHOD",
    "JSONRPC_VERSION",
    "MAX_LINE_BYTES",
    "ErrorCode",
    "ErrorObject",
    "JsonRpcError",
    "LineFramer",
    "Message",
    "Notification",
    "ProtocolError",
    "Request",
    "RequestId",
    "Response",
    "decode_binary",
    "encode_binary",
    "error_response",
    "parse_message",
    "to_wire",
]
