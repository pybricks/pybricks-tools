# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The commands themselves, run against a simulated hub.

These call the same functions the console script calls, over a real service on
a real socket or named pipe, so what is exercised is the whole path a user
takes: parse, ask the service, render. Only the radio is simulated.

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

import anyio
import pytest

import pb_service.methods.hub  # pyright: ignore[reportUnusedImport]
import pb_service.methods.program  # pyright: ignore[reportUnusedImport]

# Imported to register the RPC methods.
import pb_service.methods.service  # pyright: ignore[reportUnusedImport]
import pb_service.methods.stdio  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pb_cli import errors
from pb_cli.commands import hub as hub_commands
from pb_cli.commands import program as program_commands
from pb_cli.commands import service as service_commands
from pb_core.ipc import Endpoint
from pb_service import service as service_module
from pb_service.hub import protocol as p
from pb_service.hub.fake import FakeHub, FakeHubTransport

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
    # 0700, because the service refuses a runtime directory other users can
    # read -- its socket and its token live there. A test that made one the
    # usual way would be refused, but only on POSIX, where the check means
    # something.
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    # And a state directory of its own, so that a test choosing a hub
    # does not rewrite the choice the person running the tests made.
    monkeypatch.setenv("PB_STATE_DIR", str(tmp_path / "state"))
    # The commands resolve the endpoint from the environment, the same way the
    # console script does, so this is what points them at the private service
    # rather than at whatever is running on the developer's machine.
    monkeypatch.setenv("PB_ENDPOINT", str(endpoint))

    async with service_module.run_service(
        endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
    ):
        yield hub


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


# ------------------------------------------------------------------ hubs


async def test_connect_then_list_then_status(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await hub_commands.connect_cmd(hub=HUB_NAME) == errors.EXIT_OK
    # Connecting says so on stderr: it is a message, not data.
    captured = capsys.readouterr()
    assert HUB_NAME in captured.err
    assert captured.out == ""

    assert await hub_commands.list_cmd() == errors.EXIT_OK
    listed = _out(capsys)
    assert HUB_NAME in listed

    assert await hub_commands.status_cmd() == errors.EXIT_OK
    status = _out(capsys)
    assert f"name\t{HUB_NAME}" in status
    assert "state\tconnected" in status
    # Not shown unless asked for: the protocol version explains a refusal and
    # is noise the rest of the time.
    assert "protocol" not in status

    assert await hub_commands.status_cmd(full=True) == errors.EXIT_OK
    assert "protocol" in _out(capsys)


async def test_status_names_the_kind_of_hub(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    await hub_commands.connect_cmd(hub=HUB_NAME)
    capsys.readouterr()
    await hub_commands.status_cmd()
    assert "type\tPrime hub" in _out(capsys)


async def test_status_says_how_the_hub_is_reached(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    # Which transport a hub is on decides how quick a command is, and over USB
    # whether the hub is still advertising for anything else to find it. The
    # simulated transport says so too, rather than leaving the line out and
    # making this the one case where the answer is missing.
    await hub_commands.connect_cmd(hub=HUB_NAME)
    capsys.readouterr()
    await hub_commands.status_cmd()

    assert "connection\tsimulated" in _out(capsys)


async def test_use_remembers_a_hub_by_name(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await hub_commands.use_cmd(hub=HUB_NAME) == errors.EXIT_OK
    captured = capsys.readouterr()
    assert HUB_NAME in captured.err
    # A choice is a message, not data.
    assert captured.out == ""

    # With no terminal to choose in, the command answers the other question
    # it can answer, and the name goes to stdout for whatever asked.
    assert await hub_commands.use_cmd() == errors.EXIT_OK
    assert _out(capsys).splitlines() == [HUB_NAME]


async def test_use_accepts_a_hub_that_is_not_connected(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await hub_commands.use_cmd(hub="Workshop Hub") == errors.EXIT_OK
    err = capsys.readouterr().err
    assert "Workshop Hub" in err
    # Said out loud rather than left to be discovered.
    assert "not connected" in err


async def test_use_can_forget_the_chosen_hub(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    await hub_commands.use_cmd(hub=HUB_NAME)
    capsys.readouterr()

    assert await hub_commands.use_cmd(clear=True) == errors.EXIT_OK
    assert "No hub is chosen" in capsys.readouterr().err

    assert await hub_commands.use_cmd() == errors.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No hub is chosen" in captured.err


async def test_clearing_wins_over_a_named_hub(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    # --clear and --hub used to refuse each other. They cannot any more: a hub
    # can arrive from the environment or from the project's pyproject.toml, and
    # refusing a value the user never typed would make --clear impossible to
    # use inside a project.
    await hub_commands.use_cmd(hub=HUB_NAME)
    capsys.readouterr()

    assert (
        await hub_commands.use_cmd(hub="Some Other Hub", clear=True) == errors.EXIT_OK
    )
    assert "No hub is chosen" in capsys.readouterr().err

    assert await hub_commands.use_cmd() == errors.EXIT_OK
    assert "No hub is chosen" in capsys.readouterr().err


async def test_a_chosen_hub_is_what_a_command_acts_on(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    await hub_commands.connect_cmd(hub=HUB_NAME)
    await hub_commands.use_cmd(hub=HUB_NAME)
    capsys.readouterr()

    # The star marks the hub a command with no --hub would act on.
    assert await hub_commands.list_cmd() == errors.EXIT_OK
    listed = _out(capsys)
    assert listed.startswith("*")


async def test_scan_lists_what_it_hears(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await hub_commands.scan_cmd(timeout=0.5) == errors.EXIT_OK
    # Piped, so one plain line per hub and nothing else on stdout.
    assert _out(capsys).splitlines() == [HUB_NAME]


async def test_scan_says_so_when_it_hears_nothing(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        await hub_commands.scan_cmd(timeout=0.3, name="No Such Hub") == errors.EXIT_OK
    )
    captured = capsys.readouterr()
    assert "No hubs found" in captured.err
    # An empty answer is still an empty answer, not a blank line.
    assert captured.out == ""


async def test_disconnect_then_nothing_is_connected(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    await hub_commands.connect_cmd(hub=HUB_NAME)
    assert await hub_commands.disconnect_cmd() == errors.EXIT_OK
    capsys.readouterr()

    assert await hub_commands.list_cmd() == errors.EXIT_OK
    captured = capsys.readouterr()
    assert "No hubs connected" in captured.err
    assert captured.out == ""


async def test_asking_about_a_hub_that_is_not_connected(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    # An expected failure: one sentence and a suggestion, no traceback.
    assert await hub_commands.status_cmd(hub="No Such Hub") == errors.EXIT_HUB_ERROR
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
    await hub_commands.connect_cmd(hub=HUB_NAME)
    assert (
        await program_commands.run_cmd(_script(tmp_path), wait=False) == errors.EXIT_OK
    )
    captured = capsys.readouterr()
    assert "Program started" in captured.err
    assert captured.out == ""


async def test_run_a_one_liner(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    await hub_commands.connect_cmd(hub=HUB_NAME)
    assert await program_commands.run_cmd(command="x = 1", wait=False) == errors.EXIT_OK
    assert "Program started" in capsys.readouterr().err


async def _while_running(hub: FakeHub, prints: bytes) -> None:
    """Act out a program: print something, then end.

    Args:
        hub: The simulated hub.
        prints: What the program puts on its output before ending.
    """

    with anyio.fail_after(10):
        # A poll rather than an event, because what is being waited for is a
        # change in the simulated hub's own state, which has nothing to signal
        # with. Ruff is told so: the alternative it suggests does not exist.
        while not hub.status & p.StatusFlag.USER_PROGRAM_RUNNING:  # noqa: ASYNC110
            await anyio.sleep(0.005)

        hub.send_stdout(prints)
        await anyio.sleep(0.05)
        hub.set_program_running(False)


async def test_run_stays_with_the_program_and_prints_what_it_prints(
    service: FakeHub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = service
    await hub_commands.connect_cmd(hub=HUB_NAME)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_while_running, hub, b"counting\r\n")
        assert await program_commands.run_cmd(_script(tmp_path)) == errors.EXIT_OK

    captured = capsys.readouterr()
    # The program's own output is data, so it goes to stdout and can be piped
    # somewhere; everything pb says about the run stays on stderr.
    assert "counting" in captured.out


async def test_a_program_that_ends_in_a_traceback_fails_the_command(
    service: FakeHub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = service
    await hub_commands.connect_cmd(hub=HUB_NAME)

    # What the firmware prints for an unhandled exception. Nothing in the
    # protocol distinguishes a program that finished from one that failed --
    # the status report only stops saying that something is running -- so this
    # is the only evidence there is, and a build that runs a program on a hub
    # needs an exit code it can trust.
    traceback = (
        b"Traceback (most recent call last):\r\n"
        b'  File "__main__.py", line 3, in <module>\r\n'
        b"ZeroDivisionError: divide by zero\r\n"
    )
    code = errors.EXIT_OK

    async with anyio.create_task_group() as tg:
        tg.start_soon(_while_running, hub, traceback)
        code = await program_commands.run_cmd(_script(tmp_path))

    assert code == errors.EXIT_HUB_ERROR
    assert "ZeroDivisionError" in capsys.readouterr().out


async def test_a_program_that_simply_ends_does_not_fail_the_command(
    service: FakeHub, tmp_path: Path
) -> None:
    hub = service
    await hub_commands.connect_cmd(hub=HUB_NAME)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_while_running, hub, b"all done\r\n")
        assert await program_commands.run_cmd(_script(tmp_path)) == errors.EXIT_OK


async def test_run_reports_a_program_that_will_not_compile(
    service: FakeHub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The compiler's own diagnostics are the useful part, so they survive.
    script = _script(tmp_path, "def broken(\n")
    assert await program_commands.run_cmd(script) == errors.EXIT_ERROR
    captured = capsys.readouterr()
    assert "SyntaxError" in captured.err
    assert captured.out == ""


async def test_download_then_start_then_stop(
    service: FakeHub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await hub_commands.connect_cmd(hub=HUB_NAME)
    assert await program_commands.download_cmd(_script(tmp_path)) == errors.EXIT_OK
    assert "Downloaded" in capsys.readouterr().err

    assert await program_commands.start_cmd() == errors.EXIT_OK
    assert "Program started" in capsys.readouterr().err

    assert await program_commands.stop_cmd() == errors.EXIT_OK
    assert "Stopped" in capsys.readouterr().err


async def test_start_without_a_terminal_sets_it_going_and_returns(
    service: FakeHub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = service
    await hub_commands.connect_cmd(hub=HUB_NAME)
    assert await program_commands.download_cmd(_script(tmp_path)) == errors.EXIT_OK
    capsys.readouterr()

    # The one place pb start differs from pb run: a script starting a program
    # that is already on the hub is setting a robot going, so it returns
    # rather than holding the script open for as long as the robot runs. At a
    # terminal it would stay, which is what somebody typing it wants.
    assert await program_commands.start_cmd() == errors.EXIT_OK
    assert "Program started" in capsys.readouterr().err
    assert hub.status & p.StatusFlag.USER_PROGRAM_RUNNING


async def test_a_hub_that_refuses_to_start_says_what_to_do(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    await hub_commands.connect_cmd(hub=HUB_NAME)
    capsys.readouterr()
    service.reject_next = p.HubError.BUSY
    assert await program_commands.start_cmd() == errors.EXIT_HUB_ERROR
    captured = capsys.readouterr()
    assert "would not start" in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------- service


async def test_service_status_reports_a_running_service(
    service: FakeHub, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await service_commands.service_status() == errors.EXIT_OK
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
    # 0700, because the service refuses a runtime directory other users can
    # read -- its socket and its token live there. A test that made one the
    # usual way would be refused, but only on POSIX, where the check means
    # something.
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    # And a state directory of its own, so that a test choosing a hub
    # does not rewrite the choice the person running the tests made.
    monkeypatch.setenv("PB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PB_ENDPOINT", str(endpoint))

    assert await service_commands.service_status() == errors.EXIT_NOTHING_CONNECTED
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
    # 0700, because the service refuses a runtime directory other users can
    # read -- its socket and its token live there. A test that made one the
    # usual way would be refused, but only on POSIX, where the check means
    # something.
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    # And a state directory of its own, so that a test choosing a hub
    # does not rewrite the choice the person running the tests made.
    monkeypatch.setenv("PB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PB_ENDPOINT", str(endpoint))

    assert await service_commands.service_stop() == errors.EXIT_OK
    assert "Not running" in capsys.readouterr().err


async def test_disconnecting_when_no_service_is_running_succeeds(
    endpoint: Endpoint,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Same reasoning: nothing is connected, which is the outcome asked for.
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
    monkeypatch.setenv("PB_ENDPOINT", str(endpoint))

    assert await hub_commands.disconnect_cmd() == errors.EXIT_OK
    assert "No hub is connected" in capsys.readouterr().err


# ------------------------------------------------------------------ check


async def test_check_compiles_without_a_hub_or_a_service(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No service fixture and no hub: the point of check is that it needs
    # neither. Starting a Bluetooth connection to find out about a missing
    # bracket would be absurd.
    assert await program_commands.check_cmd(_script(tmp_path)) == errors.EXIT_OK
    captured = capsys.readouterr()
    assert "compiles" in captured.err
    assert "bytes" in captured.err
    assert captured.out == ""


async def test_check_reports_what_the_compiler_said(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _script(tmp_path, "def broken(\n")
    assert await program_commands.check_cmd(script) == errors.EXIT_ERROR
    captured = capsys.readouterr()
    assert "SyntaxError" in captured.err
    assert captured.out == ""


async def test_check_takes_a_one_liner_too(capsys: pytest.CaptureFixture[str]) -> None:
    assert await program_commands.check_cmd(command="x = 1") == errors.EXIT_OK
    assert "compiles" in capsys.readouterr().err


async def test_check_needs_exactly_one_of_a_script_and_c(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await program_commands.check_cmd() == errors.EXIT_ERROR
    assert "Give a script to check" in capsys.readouterr().err
