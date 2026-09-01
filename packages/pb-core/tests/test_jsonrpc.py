# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Message parsing and newline framing."""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from anyio.abc import ByteStream

from pb_core import jsonrpc
from pb_core.ipc.backend.memory import _pair  # pyright: ignore[reportPrivateUsage]

pytestmark = pytest.mark.anyio


# --------------------------------------------------------------- messages


def _round_trip(message: jsonrpc.Message) -> jsonrpc.Message:
    return jsonrpc.parse_message(json.loads(json.dumps(jsonrpc.to_wire(message))))


def test_request_round_trip() -> None:
    original = jsonrpc.Request(1, "hub.connect", {"name": "Pybricks Hub"})
    assert _round_trip(original) == original


def test_notification_round_trip() -> None:
    original = jsonrpc.Notification("hub.stdout", {"data": "aGk="})
    assert _round_trip(original) == original


def test_error_response_round_trip() -> None:
    original = jsonrpc.error_response(
        7, jsonrpc.ErrorCode.HUB_BUSY, "hub is busy", {"att_error": 0x81}
    )
    parsed = _round_trip(original)
    assert isinstance(parsed, jsonrpc.Response)
    assert parsed.error is not None
    assert parsed.error.code == jsonrpc.ErrorCode.HUB_BUSY
    # The raw firmware error survives, so the CLI can be specific.
    assert parsed.error.data == {"att_error": 0x81}


def test_null_result_is_preserved() -> None:
    # A success with no value must still carry "result", or it is
    # indistinguishable from an error response.
    wire = jsonrpc.to_wire(jsonrpc.Response(1, result=None))
    assert "result" in wire
    assert "error" not in wire


def test_string_ids_are_allowed() -> None:
    assert _round_trip(jsonrpc.Request("abc", "service.ping")) == jsonrpc.Request(
        "abc", "service.ping"
    )


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="not-an-object"),
        pytest.param({"jsonrpc": "1.0", "id": 1, "method": "x"}, id="wrong-version"),
        pytest.param({"jsonrpc": "2.0"}, id="neither-method-nor-id"),
        pytest.param({"jsonrpc": "2.0", "id": 1}, id="no-result-or-error"),
        pytest.param({"jsonrpc": "2.0", "id": 1, "method": 5}, id="method-not-string"),
        pytest.param(
            {"jsonrpc": "2.0", "id": True, "method": "x"}, id="bool-id-rejected"
        ),
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "method": "x", "params": [1, 2]},
            id="positional-params",
        ),
        pytest.param(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": "x", "message": "m"}},
            id="error-code-not-int",
        ),
    ],
)
def test_malformed_messages_are_rejected(payload: Any) -> None:
    with pytest.raises(jsonrpc.ProtocolError):
        jsonrpc.parse_message(payload)


def test_binary_round_trip() -> None:
    # Hub output is arbitrary bytes and may be cut mid-character, so it must
    # survive without being decoded as text anywhere.
    raw = bytes(range(256)) + b"\xff\xfe invalid utf-8 \x80"
    assert jsonrpc.decode_binary(jsonrpc.encode_binary(raw)) == raw


def test_invalid_base64_is_a_protocol_error() -> None:
    with pytest.raises(jsonrpc.ProtocolError):
        jsonrpc.decode_binary("not valid base64!!")


# ---------------------------------------------------------------- framing


async def _framed() -> tuple[
    jsonrpc.LineFramer, jsonrpc.LineFramer, ByteStream, ByteStream
]:
    left, right = _pair()
    return jsonrpc.LineFramer(left), jsonrpc.LineFramer(right), left, right


async def test_framer_round_trip() -> None:
    a, b, left, right = await _framed()

    async with left, right:
        sent = jsonrpc.Request(1, "service.ping")
        await a.send(sent)
        assert await b.receive() == sent


async def test_framer_handles_split_and_coalesced_writes() -> None:
    # A read never corresponds to one write: messages can arrive glued together
    # or cut in half, which is the classic framing bug.
    left, right = _pair()

    async with left, right:
        framer = jsonrpc.LineFramer(right)
        blob = b"".join(
            json.dumps(jsonrpc.to_wire(jsonrpc.Request(i, "m"))).encode() + b"\n"
            for i in range(3)
        )

        async with anyio.create_task_group() as tg:

            async def feed() -> None:
                for index in range(0, len(blob), 7):
                    await left.send(blob[index : index + 7])

            tg.start_soon(feed)

            for i in range(3):
                message = await framer.receive()
                assert isinstance(message, jsonrpc.Request)
                assert message.id == i


async def test_framer_reports_clean_eof() -> None:
    left, right = _pair()
    framer = jsonrpc.LineFramer(right)
    await left.aclose()

    with pytest.raises(anyio.EndOfStream):
        await framer.receive()

    await right.aclose()


async def test_framer_reports_truncated_message_as_eof() -> None:
    # A peer that dies mid-message should look the same as a clean close, so
    # callers have one shutdown path instead of two.
    left, right = _pair()
    framer = jsonrpc.LineFramer(right)
    await left.send(b'{"jsonrpc":"2.0","id":1,"me')
    await left.aclose()

    with pytest.raises(anyio.EndOfStream):
        await framer.receive()

    await right.aclose()


async def test_framer_rejects_oversized_line() -> None:
    left, right = _pair()
    framer = jsonrpc.LineFramer(right, max_line=64)

    async with anyio.create_task_group() as tg:
        tg.start_soon(left.send, b"x" * 512)

        with pytest.raises(jsonrpc.ProtocolError, match="exceeded"):
            await framer.receive()

        tg.cancel_scope.cancel()

    await left.aclose()
    await right.aclose()


async def test_framer_rejects_invalid_json() -> None:
    left, right = _pair()
    framer = jsonrpc.LineFramer(right)

    async with anyio.create_task_group() as tg:
        tg.start_soon(left.send, b"{not json}\n")

        with pytest.raises(jsonrpc.ProtocolError, match="invalid JSON"):
            await framer.receive()

        tg.cancel_scope.cancel()

    await left.aclose()
    await right.aclose()


async def test_framer_survives_a_message_containing_newline_text() -> None:
    # json.dumps escapes newlines, so an embedded newline must not split a
    # message. Cheap to get wrong with a hand-rolled encoder.
    a, b, left, right = await _framed()

    async with left, right:
        sent = jsonrpc.Notification("hub.stdout", {"text": "line one\nline two\n"})
        await a.send(sent)
        assert await b.receive() == sent
