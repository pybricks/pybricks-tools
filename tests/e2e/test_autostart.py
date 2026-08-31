# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Starting a real daemon process.

The in-process tests cover the protocol; these cover the things only a real
subprocess can show: detached spawning, the single-instance lock across
processes, and that a hub-holding daemon genuinely outlives the command that
started it.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import anyio
import anyio.to_thread
import pytest

import pb_client
from pb_core.ipc import Endpoint

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def endpoint(tmp_path: Path) -> Endpoint:
    unique = uuid.uuid4().hex[:12]
    if sys.platform == "win32":
        return Endpoint("pipe", f"pb-spawn-{unique}")
    return Endpoint("unix", str(tmp_path / f"{unique}.sock"))


@pytest.fixture(autouse=True)
def _private_runtime(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
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
    # The spawned daemon must land in the same private runtime directory.
    monkeypatch.setenv(
        "PB_DAEMON_CMD",
        f"{sys.executable} -m pb_service --idle-timeout 20",
    )
    yield


async def _stop(endpoint: Endpoint) -> None:
    """Stop the daemon if it is still running, so no test leaks a process."""
    try:
        async with pb_client.connect(endpoint, autostart=False) as daemon:
            await daemon.shutdown()
    except pb_client.DaemonUnavailable:
        pass


async def test_client_starts_a_daemon_that_outlives_it(endpoint: Endpoint) -> None:
    try:
        # First connection: nothing is listening, so a daemon is spawned.
        async with pb_client.connect(endpoint, autostart=True) as first:
            info = await first.info()
            pid = info.pid

        # The first client has now disconnected entirely. A second, separate
        # connection must reach the same process. This is the premise of the
        # whole design: state survives the command that created it.
        async with pb_client.connect(endpoint, autostart=False) as second:
            again = await second.info()
        assert again.pid == pid
        assert again.uptime_s >= info.uptime_s
    finally:
        await _stop(endpoint)


async def test_two_clients_racing_to_start_end_up_on_one_daemon(
    endpoint: Endpoint,
) -> None:
    # Deliberately uncoordinated: both may spawn, one wins the lock, and both
    # readiness loops converge on the winner.
    pids: list[int] = []

    async def connect_once() -> None:
        async with pb_client.connect(endpoint, autostart=True) as daemon:
            pids.append((await daemon.info()).pid)

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(connect_once)
            tg.start_soon(connect_once)
        assert len(pids) == 2
        assert pids[0] == pids[1], "clients reached different daemons"
    finally:
        await _stop(endpoint)


async def test_second_daemon_exits_rather_than_fighting(endpoint: Endpoint) -> None:
    try:
        async with pb_client.connect(endpoint, autostart=True) as daemon:
            await daemon.ping()

        # A second daemon on the same endpoint must decline, not crash and not
        # take over.
        # In a worker thread: subprocess.run would block the event loop.
        result = await anyio.to_thread.run_sync(
            lambda: subprocess.run(
                [sys.executable, "-m", "pb_service", "--endpoint", str(endpoint)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        )
        assert result.returncode == 3, result.stderr
    finally:
        await _stop(endpoint)


def test_cli_does_not_import_bleak() -> None:
    # The CLI must stay thin: importing the Bluetooth stack would cost startup
    # time on every invocation and break the layering.
    code = "import sys; import pb_cli.cli; print('bleak' in sys.modules, 'pb_service' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False False"
