# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

import socket
import sys
import uuid

import pytest

from pb_core.ipc.address import Endpoint


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    # Pinned to asyncio: bleak and the Windows named pipe backend are both
    # asyncio-native, so the project never runs on trio.
    return "asyncio"


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _schemes() -> list[str]:
    """Every transport that can actually run on this platform."""
    available = ["memory", "tcp"]
    available.append("pipe" if sys.platform == "win32" else "unix")
    return available


@pytest.fixture(params=_schemes())
def endpoint(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Endpoint:
    """A unique, unused endpoint for each transport under test.

    Uniqueness matters: named pipe names are machine-global, so a fixed name
    would make concurrent test runs collide.
    """
    scheme: str = request.param
    unique = uuid.uuid4().hex[:12]

    if scheme == "memory":
        return Endpoint("memory", f"test-{unique}")
    if scheme == "tcp":
        return Endpoint("tcp", f"127.0.0.1:{_free_tcp_port()}")
    if scheme == "pipe":
        return Endpoint("pipe", f"pb-test-{unique}")

    # AF_UNIX: keep the path short, since sun_path is only 104 bytes on macOS.
    directory = tmp_path_factory.mktemp("s")
    return Endpoint("unix", str(directory / f"{unique}.sock"))
