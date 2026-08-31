# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The whole stack: client library, JSON-RPC, transport, daemon.

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
import pb_service.methods.daemon  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pb_core import auth, ipc
from pb_core.ipc import Endpoint
from pb_core.jsonrpc import ErrorCode, JsonRpcError, connect_peer
from pb_core.protocol import PROTOCOL_VERSION
from pb_service import daemon as daemon_module

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
    # on, a daemon the developer happens to be running.
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
async def running_daemon(endpoint: Endpoint) -> AsyncGenerator[Endpoint, None]:
    """A daemon running in this process, so no subprocess is needed."""
    async with daemon_module.run_daemon(endpoint, idle_timeout=0):
        yield endpoint


async def test_client_can_query_a_running_daemon(running_daemon: Endpoint) -> None:
    async with pb_client.connect(running_daemon, autostart=False) as daemon:
        info = await daemon.info()
    assert info.protocol_version == PROTOCOL_VERSION
    assert info.endpoint == str(running_daemon)
    assert info.uptime_s >= 0


async def test_ping_round_trips(running_daemon: Endpoint) -> None:
    async with pb_client.connect(running_daemon, autostart=False) as daemon:
        await daemon.ping()


async def test_several_clients_share_one_daemon(running_daemon: Endpoint) -> None:
    # Multiple clients at once is the point of the daemon, so it must not be
    # accidentally single-client.
    async with (
        pb_client.connect(running_daemon, autostart=False) as first,
        pb_client.connect(running_daemon, autostart=False) as second,
    ):
        await first.ping()
        info = await second.info()
        assert info.clients == 2


async def test_connecting_without_a_daemon_reports_unavailable(
    endpoint: Endpoint,
) -> None:
    with pytest.raises(pb_client.DaemonUnavailable):
        async with pb_client.connect(endpoint, autostart=False):
            pass


async def test_a_bad_token_is_rejected(running_daemon: Endpoint) -> None:
    # Without this the default Windows named pipe permissions would let any
    # local user read a hub's output.
    async with (
        ipc.connect(running_daemon) as stream,
        connect_peer(stream) as peer,
    ):
        with pytest.raises(JsonRpcError) as info:
            await peer.call(
                "daemon.hello",
                {
                    "client": "impostor",
                    "client_version": "0",
                    "protocol_version": PROTOCOL_VERSION,
                    "token": "0" * 64,
                },
            )
        assert info.value.code == ErrorCode.NOT_AUTHENTICATED


async def test_calls_before_the_handshake_are_refused(running_daemon: Endpoint) -> None:
    async with (
        ipc.connect(running_daemon) as stream,
        connect_peer(stream) as peer,
    ):
        with pytest.raises(JsonRpcError) as info:
            await peer.call("daemon.info")
        assert info.value.code == ErrorCode.HELLO_REQUIRED


async def test_an_unsupported_protocol_version_is_refused(
    running_daemon: Endpoint,
) -> None:
    # A daemon outlives the install that started it, so a client from a newer
    # install can meet an older daemon. Both sides must say so plainly.
    async with (
        ipc.connect(running_daemon) as stream,
        connect_peer(stream) as peer,
    ):
        with pytest.raises(JsonRpcError) as info:
            await peer.call(
                "daemon.hello",
                {
                    "client": "future",
                    "client_version": "99",
                    "protocol_version": PROTOCOL_VERSION + 99,
                    "token": auth.read_or_create_token(),
                },
            )
        assert info.value.code == ErrorCode.UNSUPPORTED_PROTOCOL_VERSION


async def test_unknown_methods_are_reported(running_daemon: Endpoint) -> None:
    async with pb_client.connect(running_daemon, autostart=False) as daemon:
        with pytest.raises(JsonRpcError) as info:
            await daemon._peer.call("no.such.method")  # pyright: ignore[reportPrivateUsage]
        assert info.value.code == ErrorCode.METHOD_NOT_FOUND


async def test_shutdown_stops_the_daemon(endpoint: Endpoint) -> None:
    async with daemon_module.run_daemon(endpoint, idle_timeout=0) as daemon:
        async with pb_client.connect(endpoint, autostart=False) as client:
            await client.shutdown()
        with anyio.fail_after(5):
            await daemon.wait_for_shutdown()


async def test_idle_daemon_exits_on_its_own(endpoint: Endpoint) -> None:
    # A connected hub will keep it alive; with nothing to hold open it should
    # not linger.
    async with daemon_module.run_daemon(endpoint, idle_timeout=0.5) as daemon:
        with anyio.fail_after(10):
            await daemon.wait_for_shutdown()
    assert "idle" in daemon.shutdown_reason
