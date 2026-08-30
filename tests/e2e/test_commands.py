# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The commands themselves, run against a simulated hub.

These call the same functions the console script calls, over a real service on
a real socket or named pipe, so what is exercised is the whole path a user
takes: parse, ask the daemon, render. Only the radio is simulated.

Two things are asserted throughout, because they are the promises easiest to
break by accident. Every command returns the documented exit code, and
**nothing but data ever reaches stdout** -- progress, messages and errors all
belong on stderr, which is what keeps ``pb list | jq`` and ``pb scan > hubs``
working.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from pb import commands_hub, commands_program, commands_service, errors
from pb_core.ipc import Endpoint
from pbd import daemon as daemon_module
from pbd import (  # noqa: F401  (imported to register the RPC methods)
    methods_daemon,  # pyright: ignore[reportUnusedImport]
    methods_hub,  # pyright: ignore[reportUnusedImport]
    methods_program,  # pyright: ignore[reportUnusedImport]
)
from pbd.hub import protocol as p
from pbd.hub.fake import FakeHub, FakeHubTransport

pytestmark = pytest.mark.anyio

HUB_NAME = "Pybricks Hub"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub(name=HUB_NAME, address="00:11:22:33:44:55")


@pytest.fixture
def endpoint(tmp_path: Path) -> Endpoint:
    unique = uuid.uuid4().hex[:12]
    if sys.platform == "win32":
        return Endpoint("pipe", f"pb-cmd-{unique}")
    return Endpoint("unix", str(tmp_path / f"{unique}.sock"))


@pytest.fixture
async def service(
    endpoint: Endpoint, hub: FakeHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncGenerator[FakeHub, None]:
    """Run a service the commands will find, without touching the real one.

    In this very event loop. The commands are ``async def`` and cyclopts runs
    them, so nothing here starts a loop of its own and there is no thread or
    blocking portal between the test and what it is testing.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    # The commands resolve the endpoint from the environment, the same way the
    # console script does, so this is what points them at the private service
    # rather than at whatever is running on the developer's machine.
    monkeypatch.setenv("PB_ENDPOINT", str(endpoint))

    async with daemon_module.run_daemon(
        endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
    ):
        yield hub


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


# ------------------------------------------------------------------ hubs


async def test_connect_then_list_then_status(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await commands_hub.connect_cmd(hub=HUB_NAME) == errors.EXIT_OK
    # Connecting says so on stderr: it is a message, not data.
    captured = capsys.readouterr()
    assert HUB_NAME in captured.err
    assert captured.out == ""

    assert await commands_hub.list_cmd() == errors.EXIT_OK
    listed = _out(capsys)
    assert HUB_NAME in listed

    assert await commands_hub.status_cmd() == errors.EXIT_OK
    status = _out(capsys)
    assert f"name\t{HUB_NAME}" in status
    assert "state\tconnected" in status
    # Not shown unless asked for: the protocol version explains a refusal and
    # is noise the rest of the time.
    assert "protocol" not in status

    assert await commands_hub.status_cmd(full=True) == errors.EXIT_OK
    assert "protocol" in _out(capsys)


async def test_status_names_the_kind_of_hub(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands_hub.connect_cmd(hub=HUB_NAME)
    capsys.readouterr()
    await commands_hub.status_cmd()
    assert "type\tPrime hub" in _out(capsys)


async def test_scan_lists_what_it_hears(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await commands_hub.scan_cmd(timeout=0.5) == errors.EXIT_OK
    # Piped, so one plain line per hub and nothing else on stdout.
    assert _out(capsys).splitlines() == [HUB_NAME]


async def test_scan_says_so_when_it_hears_nothing(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        await commands_hub.scan_cmd(timeout=0.3, name="No Such Hub") == errors.EXIT_OK
    )
    captured = capsys.readouterr()
    assert "No hubs found" in captured.err
    # An empty answer is still an empty answer, not a blank line.
    assert captured.out == ""


async def test_disconnect_then_nothing_is_connected(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands_hub.connect_cmd(hub=HUB_NAME)
    assert await commands_hub.disconnect_cmd() == errors.EXIT_OK
    capsys.readouterr()

    assert await commands_hub.list_cmd() == errors.EXIT_OK
    captured = capsys.readouterr()
    assert "No hubs connected" in captured.err
    assert captured.out == ""


async def test_asking_about_a_hub_that_is_not_connected(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    # An expected failure: one sentence and a suggestion, no traceback.
    assert await commands_hub.status_cmd(hub="No Such Hub") == errors.EXIT_HUB_ERROR
    captured = capsys.readouterr()
    assert "pb connect" in captured.err
    assert captured.out == ""


# --------------------------------------------------------------- programs


def _script(tmp_path: Path, source: str = "print('hi')\n") -> Path:
    path = tmp_path / "demo.py"
    path.write_text(source, encoding="utf-8")
    return path


async def test_run_a_script(
    service: FakeHub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands_hub.connect_cmd(hub=HUB_NAME)
    assert (
        await commands_program.run_cmd(_script(tmp_path), wait=False) == errors.EXIT_OK
    )
    captured = capsys.readouterr()
    assert "Program started" in captured.err
    assert captured.out == ""


async def test_run_a_one_liner(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands_hub.connect_cmd(hub=HUB_NAME)
    assert await commands_program.run_cmd(command="x = 1", wait=False) == errors.EXIT_OK
    assert "Program started" in capsys.readouterr().err


async def test_run_reports_a_program_that_will_not_compile(
    service: FakeHub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The compiler's own diagnostics are the useful part, so they survive.
    script = _script(tmp_path, "def broken(\n")
    assert await commands_program.run_cmd(script) == errors.EXIT_ERROR
    captured = capsys.readouterr()
    assert "SyntaxError" in captured.err
    assert captured.out == ""


async def test_download_then_start_then_stop(
    service: FakeHub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands_hub.connect_cmd(hub=HUB_NAME)
    assert await commands_program.download_cmd(_script(tmp_path)) == errors.EXIT_OK
    assert "Downloaded" in capsys.readouterr().err

    assert await commands_program.start_cmd() == errors.EXIT_OK
    assert "Program started" in capsys.readouterr().err

    assert await commands_program.stop_cmd() == errors.EXIT_OK
    assert "Stopped" in capsys.readouterr().err


async def test_a_hub_that_refuses_to_start_says_what_to_do(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    await commands_hub.connect_cmd(hub=HUB_NAME)
    capsys.readouterr()
    service.reject_next = p.HubError.BUSY
    assert await commands_program.start_cmd() == errors.EXIT_HUB_ERROR
    captured = capsys.readouterr()
    assert "would not start" in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------- service


async def test_service_status_reports_a_running_service(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await commands_service.service_status() == errors.EXIT_OK
    reported = _out(capsys)
    assert "status\trunning" in reported
    # The log path is the single most useful thing here, because the service
    # has no terminal of its own to complain to.
    assert "log\t" in reported


async def test_service_status_when_nothing_is_running(
    endpoint: Endpoint,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # No service fixture: nothing is listening. "It is not running" answers
    # the question rather than failing to answer it, so it says so plainly
    # and exits with the code that means nothing is connected.
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("PB_ENDPOINT", str(endpoint))

    assert await commands_service.service_status() == errors.EXIT_NOTHING_CONNECTED
    captured = capsys.readouterr()
    assert "Not running" in captured.err
    assert captured.out == ""


async def test_stopping_a_service_that_is_not_running_succeeds(
    endpoint: Endpoint,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Asking for something to be stopped when it already is got what was
    # wanted, so it is not an error.
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("PB_ENDPOINT", str(endpoint))

    assert await commands_service.service_stop() == errors.EXIT_OK
    assert "Not running" in capsys.readouterr().err


async def test_disconnecting_when_no_service_is_running_succeeds(
    endpoint: Endpoint,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Same reasoning: nothing is connected, which is the outcome asked for.
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("PB_ENDPOINT", str(endpoint))

    assert await commands_hub.disconnect_cmd() == errors.EXIT_OK
    assert "No hub is connected" in capsys.readouterr().err


# ------------------------------------------------------------------ check


async def test_check_compiles_without_a_hub_or_a_service(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No service fixture and no hub: the point of check is that it needs
    # neither. Starting a Bluetooth connection to find out about a missing
    # bracket would be absurd.
    assert await commands_program.check_cmd(_script(tmp_path)) == errors.EXIT_OK
    captured = capsys.readouterr()
    assert "compiles" in captured.err
    assert "bytes" in captured.err
    assert captured.out == ""


async def test_check_reports_what_the_compiler_said(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _script(tmp_path, "def broken(\n")
    assert await commands_program.check_cmd(script) == errors.EXIT_ERROR
    captured = capsys.readouterr()
    assert "SyntaxError" in captured.err
    assert captured.out == ""


async def test_check_takes_a_one_liner_too(capsys: pytest.CaptureFixture[str]) -> None:
    assert await commands_program.check_cmd(command="x = 1") == errors.EXIT_OK
    assert "compiles" in capsys.readouterr().err


async def test_check_needs_exactly_one_of_a_script_and_c(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await commands_program.check_cmd() == errors.EXIT_ERROR
    assert "Give a script to check" in capsys.readouterr().err
