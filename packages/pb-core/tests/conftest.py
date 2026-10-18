# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

import shutil
import socket
import sys
import tempfile
import uuid
from collections.abc import Iterator

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
def endpoint(request: pytest.FixtureRequest) -> Iterator[Endpoint]:
    """A unique, unused endpoint for each transport under test.

    Uniqueness matters: named pipe names are machine-global, so a fixed name
    would make concurrent test runs collide.
    """
    scheme: str = request.param
    unique = uuid.uuid4().hex[:12]

    if scheme == "memory":
        yield Endpoint("memory", f"test-{unique}")
        return

    if scheme == "tcp":
        yield Endpoint("tcp", f"127.0.0.1:{_free_tcp_port()}")
        return

    if scheme == "pipe":
        yield Endpoint("pipe", f"pb-test-{unique}")
        return

    # AF_UNIX: under /tmp rather than pytest's tmp_path, since sun_path is only
    # 104 bytes on macOS. The per-user temporary directory there is 57 bytes
    # once resolved, and with pytest's own two levels a socket in it measures
    # 104 as soon as pytest's run counter reaches three digits -- one too many,
    # counting the terminator. tests/e2e/conftest.py's short_tmp_path does the
    # same for the end-to-end tests.
    directory = tempfile.mkdtemp(prefix="pb-", dir="/tmp")
    try:
        yield Endpoint("unix", f"{directory}/{unique}.sock")
    finally:
        shutil.rmtree(directory, ignore_errors=True)
