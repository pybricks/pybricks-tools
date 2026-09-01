# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The whole stack: client library, JSON-RPC, transport, service.

These run against the real transports rather than a stub, so the platform
specific code is exercised on every OS in CI.
"""

from __future__ import annotations

import socket
import sys
import uuid
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import anyio
import pytest

import pb_client

# Imported to register the RPC methods.
import pb_service.methods.service  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pb_core import auth, ipc
from pb_core.ipc import Endpoint
from pb_core.jsonrpc import ErrorCode, JsonRpcError, connect_peer
from pb_core.protocol import PROTOCOL_VERSION
from pb_service import service as service_module

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(params=["tcp", "native"])
def endpoint(request: pytest.FixtureRequest, tmp_path: Path) -> Endpoint:
    """A private endpoint per test, on TCP and on the platform's own transport."""

    if request.param == "tcp":
        return Endpoint("tcp", f"127.0.0.1:{_free_port()}")

    unique = uuid.uuid4().hex[:12]

    if sys.platform == "win32":
        return Endpoint("pipe", f"pb-e2e-{unique}")

    return Endpoint("unix", str(tmp_path / f"{unique}.sock"))


@pytest.fixture(autouse=True)
def _private_runtime(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    # Redirect the lock, cookie and socket so tests never disturb, or depend
    # on, a service the developer happens to be running.
    runtime = tmp_path / "runtime"
    # 0700, because the service refuses a runtime directory other users can
    # read -- its socket and its token live there. A test that made one the
    # usual way would be refused, but only on POSIX, where the check means
    # something.
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    # And a state directory of its own, so that a test choosing a hub
    # does not rewrite the choice the person running the tests made.
    monkeypatch.setenv("PB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("PB_ENDPOINT", raising=False)
    yield


@pytest.fixture
async def running_service(endpoint: Endpoint) -> AsyncGenerator[Endpoint, None]:
    """A service running in this process, so no subprocess is needed."""

    async with service_module.run_service(endpoint, idle_timeout=0):
        yield endpoint


async def test_client_can_query_a_running_service(running_service: Endpoint) -> None:
    async with pb_client.connect(running_service, autostart=False) as service:
        info = await service.info()

    assert info.protocol_version == PROTOCOL_VERSION
    assert info.endpoint == str(running_service)
    assert info.uptime_s >= 0


async def test_ping_round_trips(running_service: Endpoint) -> None:
    async with pb_client.connect(running_service, autostart=False) as service:
        await service.ping()


async def test_several_clients_share_one_service(running_service: Endpoint) -> None:
    # Multiple clients at once is the point of the service, so it must not be
    # accidentally single-client.
    async with (
        pb_client.connect(running_service, autostart=False) as first,
        pb_client.connect(running_service, autostart=False) as second,
    ):
        await first.ping()
        info = await second.info()
        assert info.clients == 2


async def test_connecting_without_a_service_reports_unavailable(
    endpoint: Endpoint,
) -> None:
    with pytest.raises(pb_client.ServiceUnavailable):
        async with pb_client.connect(endpoint, autostart=False):
            pass


async def test_a_bad_token_is_rejected(running_service: Endpoint) -> None:
    # Without this the default Windows named pipe permissions would let any
    # local user read a hub's output.
    async with (
        ipc.connect(running_service) as stream,
        connect_peer(stream) as peer,
    ):
        with pytest.raises(JsonRpcError) as info:
            await peer.call(
                "service.hello",
                {
                    "client": "impostor",
                    "client_version": "0",
                    "protocol_version": PROTOCOL_VERSION,
                    "token": "0" * 64,
                },
            )

        assert info.value.code == ErrorCode.NOT_AUTHENTICATED


async def test_calls_before_the_handshake_are_refused(
    running_service: Endpoint,
) -> None:
    async with (
        ipc.connect(running_service) as stream,
        connect_peer(stream) as peer,
    ):
        with pytest.raises(JsonRpcError) as info:
            await peer.call("service.info")

        assert info.value.code == ErrorCode.HELLO_REQUIRED


async def test_an_unsupported_protocol_version_is_refused(
    running_service: Endpoint,
) -> None:
    # A service outlives the install that started it, so a client from a newer
    # install can meet an older service. Both sides must say so plainly.
    async with (
        ipc.connect(running_service) as stream,
        connect_peer(stream) as peer,
    ):
        with pytest.raises(JsonRpcError) as info:
            await peer.call(
                "service.hello",
                {
                    "client": "future",
                    "client_version": "99",
                    "protocol_version": PROTOCOL_VERSION + 99,
                    "token": auth.read_or_create_token(),
                },
            )

        assert info.value.code == ErrorCode.UNSUPPORTED_PROTOCOL_VERSION


async def test_unknown_methods_are_reported(running_service: Endpoint) -> None:
    async with pb_client.connect(running_service, autostart=False) as service:
        with pytest.raises(JsonRpcError) as info:
            await service._peer.call("no.such.method")  # pyright: ignore[reportPrivateUsage]

        assert info.value.code == ErrorCode.METHOD_NOT_FOUND


async def test_shutdown_stops_the_service(endpoint: Endpoint) -> None:
    async with service_module.run_service(endpoint, idle_timeout=0) as service:
        async with pb_client.connect(endpoint, autostart=False) as client:
            await client.shutdown()

        with anyio.fail_after(5):
            await service.wait_for_shutdown()


async def test_idle_service_exits_on_its_own(endpoint: Endpoint) -> None:
    # A connected hub will keep it alive; with nothing to hold open it should
    # not linger.
    #
    # Three tenths of a second deliberately: it is not a multiple of the 250 ms
    # tick this used to wake on, which would have rounded it up to a half. A
    # wait for the deadline itself cannot round it up, and the service's own
    # clock is what to measure against, so that starting the listener does not
    # count as idle time.
    async with service_module.run_service(endpoint, idle_timeout=0.3) as service:
        with anyio.fail_after(10):
            await service.wait_for_shutdown()

        took = service.uptime

    assert "idle" in service.shutdown_reason
    assert 0.3 <= took < 0.45, f"exited after {took:.3f}s, wanted about 0.3s"
