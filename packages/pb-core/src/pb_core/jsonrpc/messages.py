# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

r"""JSON-RPC 2.0 message types and their wire encoding.

Binary payloads travel as base64 strings. That is a deliberate choice over
latin-1, which round-trips but makes JSON escape every byte above 0x7f as
``\\u00XX`` -- six bytes per byte, worse than base64's 1.33x, and a trap for any
client not written in Python. It also matters that hub output is arbitrary
bytes: it can be cut mid-character, so the service must never try to decode it.
Base64 pushes decoding to the client, which owns the terminal and knows the
encoding it wants.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Final, TypeAlias, cast

from pb_core.jsonrpc.errors import ErrorCode, ProtocolError

__all__ = [
    "CANCEL_METHOD",
    "JSONRPC_VERSION",
    "ErrorObject",
    "Message",
    "Notification",
    "Request",
    "RequestId",
    "Response",
    "decode_binary",
    "encode_binary",
    "parse_message",
    "to_wire",
]

JSONRPC_VERSION: Final = "2.0"

CANCEL_METHOD: Final = "$/cancelRequest"
"""Cancellation notification.

JSON-RPC has no cancellation of its own, so this borrows the Language Server
Protocol's convention. It matters because a client-side deadline only abandons
the client's wait; without telling the service, in-flight work would continue.
"""

RequestId: TypeAlias = int | str


def encode_binary(data: bytes) -> str:
    """Encode bytes for transport in a JSON string.

    Args:
        data: Raw bytes.

    Returns:
        Standard base64 with padding.
    """
    return base64.b64encode(data).decode("ascii")


def decode_binary(text: str) -> bytes:
    """Decode a base64 field back to bytes.

    Args:
        text: The base64 text from the wire.

    Returns:
        The decoded bytes.

    Raises:
        ProtocolError: If the field is not valid base64.
    """

    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"invalid base64 payload: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ErrorObject:
    """The ``error`` member of a failed response."""

    code: int
    message: str
    data: Any = None


@dataclass(frozen=True, slots=True)
class Request:
    """A call that expects a response."""

    id: RequestId
    method: str
    params: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Notification:
    """A one-way message. Never answered, and may be dropped under load."""

    method: str
    params: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Response:
    """A reply to a :class:`Request`. Exactly one of the payload fields is set."""

    id: RequestId | None
    result: Any = None
    error: ErrorObject | None = None


Message: TypeAlias = Request | Notification | Response


def to_wire(message: Message) -> dict[str, Any]:
    """Convert a message to the dictionary that is serialized to JSON.

    Args:
        message: The message to encode.

    Returns:
        A JSON-serializable dictionary.
    """

    if isinstance(message, Request):
        wire: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": message.id,
            "method": message.method,
        }

        if message.params is not None:
            wire["params"] = message.params

        return wire

    if isinstance(message, Notification):
        wire = {"jsonrpc": JSONRPC_VERSION, "method": message.method}

        if message.params is not None:
            wire["params"] = message.params

        return wire

    wire = {"jsonrpc": JSONRPC_VERSION, "id": message.id}

    if message.error is not None:
        error: dict[str, Any] = {
            "code": message.error.code,
            "message": message.error.message,
        }

        if message.error.data is not None:
            error["data"] = message.error.data

        wire["error"] = error
    else:
        # A successful response must carry "result" even when it is null.
        wire["result"] = message.result

    return wire


def _require_id(raw: object) -> RequestId:
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        raise ProtocolError(f"invalid request id: {raw!r}")

    return raw


def _params(raw: object) -> dict[str, Any] | None:
    if raw is None:
        return None

    if not isinstance(raw, dict):
        # Positional parameters are legal JSON-RPC but this protocol always
        # uses named ones, so that adding a field is never a breaking change.
        raise ProtocolError("params must be an object")

    return cast("dict[str, Any]", raw)


def parse_message(raw: object) -> Message:
    """Parse a decoded JSON value into a message.

    Args:
        raw: The result of ``json.loads`` for one line.

    Returns:
        The parsed message.

    Raises:
        ProtocolError: If the value is not a well-formed JSON-RPC 2.0 message.
    """

    if not isinstance(raw, dict):
        raise ProtocolError("message must be a JSON object")

    obj = cast("dict[str, Any]", raw)

    if obj.get("jsonrpc") != JSONRPC_VERSION:
        raise ProtocolError(f"unsupported jsonrpc version: {obj.get('jsonrpc')!r}")

    has_method = "method" in obj
    has_id = "id" in obj

    if has_method:
        method = obj["method"]
        if not isinstance(method, str):
            raise ProtocolError("method must be a string")

        params = _params(obj.get("params"))

        if has_id:
            return Request(_require_id(obj["id"]), method, params)

        return Notification(method, params)

    if not has_id:
        raise ProtocolError("message has neither method nor id")

    # A response to an unparseable request carries a null id.
    identifier = None if obj["id"] is None else _require_id(obj["id"])

    if "error" in obj:
        raw_error = obj["error"]
        if not isinstance(raw_error, dict):
            raise ProtocolError("error must be an object")

        error = cast("dict[str, Any]", raw_error)
        code = error.get("code")
        message = error.get("message")

        if not isinstance(code, int) or isinstance(code, bool):
            raise ProtocolError("error.code must be an integer")

        if not isinstance(message, str):
            raise ProtocolError("error.message must be a string")

        return Response(identifier, error=ErrorObject(code, message, error.get("data")))

    if "result" not in obj:
        raise ProtocolError("response has neither result nor error")

    return Response(identifier, result=obj["result"])


def error_response(
    identifier: RequestId | None,
    code: ErrorCode | int,
    message: str,
    data: Any = None,
) -> Response:
    """Build an error response.

    Args:
        identifier: The id of the failed request, or ``None`` if it could not
            be determined.
        code: The error code.
        message: Short human-readable description.
        data: Structured detail.

    Returns:
        The response to send.
    """
    return Response(identifier, error=ErrorObject(int(code), message, data))
