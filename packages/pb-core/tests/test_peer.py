# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The JSON-RPC peer: calls, notifications, cancellation and shutdown."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any

import anyio
import anyio.lowlevel
import pytest

from pb_core import jsonrpc
from pb_core.ipc.backend.memory import _pair  # pyright: ignore[reportPrivateUsage]
from pb_core.jsonrpc.peer import (
    OUTBOX_SIZE,
    JsonRpcPeer,
    PeerClosedError,
    connect_peer,
)

pytestmark = pytest.mark.anyio


@contextlib.asynccontextmanager
async def _connected(
    server_handler: jsonrpc.MethodHandler | None = None,
) -> AsyncGenerator[tuple[JsonRpcPeer, JsonRpcPeer], None]:
    """A client and a server peer joined by an in-process stream."""
    client_stream, server_stream = _pair()
    async with (
        client_stream,
        server_stream,
        connect_peer(server_stream, server_handler) as server,
        connect_peer(client_stream) as client,
    ):
        yield client, server


async def _echo_handler(request: jsonrpc.Request) -> Any:
    if request.method == "ping":
        return {"pong": True}
    if request.method == "echo":
        return request.params
    if request.method == "boom":
        raise RuntimeError("handler exploded")
    if request.method == "busy":
        raise jsonrpc.JsonRpcError(
            jsonrpc.ErrorCode.HUB_BUSY, "hub is busy", {"att_error": 0x81}
        )
    raise jsonrpc.JsonRpcError(jsonrpc.ErrorCode.METHOD_NOT_FOUND, request.method)


async def test_call_returns_result() -> None:
    async with _connected(_echo_handler) as (client, _):
        assert await client.call("ping") == {"pong": True}


async def test_params_round_trip() -> None:
    async with _connected(_echo_handler) as (client, _):
        assert await client.call("echo", {"a": 1, "b": "two"}) == {"a": 1, "b": "two"}


async def test_handler_error_becomes_typed_error() -> None:
    async with _connected(_echo_handler) as (client, _):
        with pytest.raises(jsonrpc.JsonRpcError) as info:
            await client.call("busy")
        assert info.value.code == jsonrpc.ErrorCode.HUB_BUSY
        # The raw firmware code survives the round trip.
        assert info.value.data == {"att_error": 0x81}


async def test_unexpected_handler_exception_becomes_internal_error() -> None:
    async with _connected(_echo_handler) as (client, _):
        with pytest.raises(jsonrpc.JsonRpcError) as info:
            await client.call("boom")
        assert info.value.code == jsonrpc.ErrorCode.INTERNAL_ERROR
        assert "handler exploded" in info.value.message


async def test_concurrent_calls_are_matched_to_their_responses() -> None:
    # Responses may arrive in any order, so ids must be matched, not assumed.
    async def slow_handler(request: jsonrpc.Request) -> Any:
        params = request.params or {}
        await anyio.sleep(float(params.get("delay", 0)))
        return params.get("tag")

    results: dict[str, Any] = {}

    async with _connected(slow_handler) as (client, _):

        async def call(tag: str, delay: float) -> None:
            results[tag] = await client.call("x", {"tag": tag, "delay": delay})

        async with anyio.create_task_group() as tg:
            tg.start_soon(call, "slow", 0.05)
            tg.start_soon(call, "fast", 0.0)

    assert results == {"slow": "slow", "fast": "fast"}


async def test_notification_reaches_the_handler() -> None:
    seen: list[str] = []
    received = anyio.Event()

    async def handler(request: jsonrpc.Request) -> Any:
        seen.append(request.method)
        received.set()
        return None

    async with _connected(handler) as (client, _):
        client.notify("hub.stdout", {"data": "aGk="})
        with anyio.fail_after(5):
            await received.wait()

    assert seen == ["hub.stdout"]


async def test_cancelling_a_call_tells_the_peer_to_stop() -> None:
    # A client-side deadline only abandons this side's wait, so without
    # $/cancelRequest the service would keep working on an abandoned request.
    started = anyio.Event()
    cancelled = anyio.Event()

    async def handler(request: jsonrpc.Request) -> Any:
        started.set()
        try:
            await anyio.sleep(30)
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            raise
        return "never"

    async with _connected(handler) as (client, _):
        with anyio.move_on_after(0.5) as scope:
            async with anyio.create_task_group() as tg:
                tg.start_soon(client.call, "slow")
                with anyio.fail_after(5):
                    await started.wait()
                await anyio.sleep(0.05)
                tg.cancel_scope.cancel()
        assert not scope.cancelled_caught, "cancellation should have been prompt"

        with anyio.fail_after(5):
            await cancelled.wait()


async def test_pending_calls_fail_when_the_connection_drops() -> None:
    # Otherwise a caller waits forever on a service that has gone away.
    client_stream, server_stream = _pair()
    async with (
        client_stream,
        connect_peer(client_stream) as client,
        anyio.create_task_group() as tg,
    ):

        async def call_and_record() -> None:
            with pytest.raises(PeerClosedError):
                await client.call("anything")

        tg.start_soon(call_and_record)
        await anyio.sleep(0.05)
        await server_stream.aclose()


async def test_call_against_a_peer_with_no_handler_is_method_not_found() -> None:
    async with _connected(None) as (client, _):
        with pytest.raises(jsonrpc.JsonRpcError) as info:
            await client.call("anything")
        assert info.value.code == jsonrpc.ErrorCode.METHOD_NOT_FOUND


async def test_notifications_are_dropped_rather_than_blocking() -> None:
    # The hub notification path must never be able to block, so an outbox that
    # nobody is draining must shed rather than apply backpressure.
    client_stream, server_stream = _pair()
    async with client_stream, server_stream, connect_peer(client_stream) as client:
        for index in range(OUTBOX_SIZE + 200):
            client.notify("flood", {"n": index})
    assert client.dropped_notifications > 0


async def test_no_tasks_outlive_the_connection() -> None:
    # The black-box rule, asserted directly: connect_peer must not leave a
    # reader, writer or handler task running after its block exits.
    import asyncio

    before = len(asyncio.all_tasks())
    async with _connected(_echo_handler) as (client, _):
        await client.call("ping")
    for _ in range(10):
        await anyio.lowlevel.checkpoint()
    assert len(asyncio.all_tasks()) == before
