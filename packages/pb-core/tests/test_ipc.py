# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Conformance suite run against every transport available on this platform.

The same test bodies run against the in-process memory backend everywhere and
against the real socket, named pipe or TCP transport on its native platform, so
the platform-specific code is exercised for real rather than only reasoned about.
"""

from __future__ import annotations

import sys

import anyio
import pytest
from anyio.abc import ByteStream, TaskGroup, TaskStatus

from pb_core import ipc
from pb_core.ipc.address import Endpoint

pytestmark = pytest.mark.anyio


async def _echo(stream: ByteStream) -> None:
    """Echo until the peer goes away."""
    try:
        async for chunk in stream:
            await stream.send(chunk)
    except (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
        pass


async def _receive_exactly(stream: ByteStream, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        buf.extend(await stream.receive(count - len(buf)))
    return bytes(buf)


async def _serve(endpoint: Endpoint, task_group: TaskGroup) -> ipc.IpcListener:
    """Start an echo server owned by ``task_group``."""
    listener = await task_group.start(_run_echo_server, endpoint)
    assert isinstance(listener, ipc.IpcListener)
    return listener


async def _run_echo_server(
    endpoint: Endpoint, *, task_status: TaskStatus[ipc.IpcListener]
) -> None:
    async with (
        ipc.create_listener(endpoint) as listener,
        anyio.create_task_group() as tg,
    ):
        task_status.started(listener)
        await listener.serve(_echo, tg)


async def test_round_trip(endpoint: Endpoint) -> None:
    async with anyio.create_task_group() as tg:
        await _serve(endpoint, tg)
        async with ipc.connect(endpoint) as client:
            greeting = b"hello service"
            await client.send(greeting)
            assert await _receive_exactly(client, len(greeting)) == greeting
        tg.cancel_scope.cancel()


async def test_large_message(endpoint: Endpoint) -> None:
    # Firmware images and program blobs are chunked, but the transport must
    # still cope with a payload far larger than one read.
    payload = bytes(range(256)) * 16384  # 4 MiB
    async with anyio.create_task_group() as tg:
        await _serve(endpoint, tg)
        async with ipc.connect(endpoint) as client:

            async def send_all() -> None:
                await client.send(payload)

            async with anyio.create_task_group() as inner:
                inner.start_soon(send_all)
                assert await _receive_exactly(client, len(payload)) == payload
        tg.cancel_scope.cancel()


async def test_many_concurrent_clients(endpoint: Endpoint) -> None:
    results: list[bytes] = []

    async def one(index: int) -> None:
        async with ipc.connect(endpoint) as client:
            message = f"client-{index:02d}".encode()
            await client.send(message)
            results.append(await _receive_exactly(client, len(message)))

    async with anyio.create_task_group() as tg:
        await _serve(endpoint, tg)
        async with anyio.create_task_group() as clients:
            for index in range(10):
                clients.start_soon(one, index)
        tg.cancel_scope.cancel()

    assert sorted(results) == sorted(f"client-{i:02d}".encode() for i in range(10))


async def test_server_sees_client_disconnect(endpoint: Endpoint) -> None:
    seen: list[str] = []
    noticed = anyio.Event()

    async def handler(stream: ByteStream) -> None:
        try:
            await stream.receive()
        except anyio.EndOfStream:
            seen.append("eof")
        except anyio.BrokenResourceError:
            seen.append("broken")
        finally:
            noticed.set()

    async def server(*, task_status: TaskStatus[None]) -> None:
        async with (
            ipc.create_listener(endpoint) as listener,
            anyio.create_task_group() as tg,
        ):
            task_status.started()
            await listener.serve(handler, tg)

    async with anyio.create_task_group() as tg:
        await tg.start(server)
        async with ipc.connect(endpoint):
            pass  # close immediately without sending
        with anyio.fail_after(5):
            await noticed.wait()
        tg.cancel_scope.cancel()

    # Either signal is a correct report of "the peer went away".
    assert seen[0] in ("eof", "broken")


async def test_connect_without_listener_is_refused(endpoint: Endpoint) -> None:
    # This is how the client decides the service is not running and starts one,
    # so it must be prompt and must be this exact exception type.
    with anyio.fail_after(10), pytest.raises(ConnectionRefusedError):
        async with ipc.connect(endpoint):
            pass


async def test_second_listener_is_rejected(endpoint: Endpoint) -> None:
    # Every backend reports this differently at the OS level -- PermissionError
    # from FILE_FLAG_FIRST_PIPE_INSTANCE, EADDRINUSE, WSAEADDRINUSE -- so the
    # point of this test is that callers see one normalised exception.
    async with anyio.create_task_group() as tg:
        await _serve(endpoint, tg)
        with pytest.raises(ipc.AddressInUseError):
            async with ipc.create_listener(endpoint):
                pass
        tg.cancel_scope.cancel()


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows-only")
async def test_pipe_cannot_half_close(endpoint: Endpoint) -> None:
    if endpoint.scheme != "pipe":
        pytest.skip("only meaningful for the pipe transport")
    async with anyio.create_task_group() as tg:
        await _serve(endpoint, tg)
        async with ipc.connect(endpoint) as client:
            with pytest.raises(NotImplementedError):
                await client.send_eof()
        tg.cancel_scope.cancel()
