# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""An interactive session with a hub, end to end against a simulated one.

Both ways in are here: ``pb repl``, which talks to the hub's interpreter, and
``pb run --interactive`` / ``pb start --interactive``, which talk to a program.
They share everything below the command, so they are tested against the same
scripted terminal.

The whole path a user takes runs here: the command, a real service over a real
socket or named pipe, the attach subscription, keystrokes going back, and the
status line drawing on a terminal that only exists in this file. Only the radio
and the hub itself are simulated.

The terminal is where the interesting seams are, so it is stood in for rather
than mocked away: :class:`_Screen` keeps every byte the session writes, which
is what lets a test say "the hub's output reached the screen" and "the
scrolling margin was given back" rather than "the right method was called".

Keys are held back until a test says so. Without that the session would read
its whole script and leave before the hub had done anything, which is not a
race a real user can lose but is one a test wins every time.
"""

from __future__ import annotations

import contextlib
import sys
import uuid
from collections.abc import AsyncGenerator, Callable, Coroutine, Generator, Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest

# Imported to register the RPC methods.
import pbd.methods.daemon  # pyright: ignore[reportUnusedImport]
import pbd.methods.hub  # pyright: ignore[reportUnusedImport]
import pbd.methods.program  # pyright: ignore[reportUnusedImport]
import pbd.methods.stdio  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pb import console as console_module
from pb import errors, escape, session
from pb.commands import hub as hub_commands
from pb.commands import program as program_commands
from pb.commands import repl as repl_command
from pb_core.ipc import Endpoint
from pbd import daemon as daemon_module
from pbd.hub import protocol as p
from pbd.hub.fake import FakeHub, FakeHubTransport

pytestmark = pytest.mark.anyio

HUB_NAME = "Pybricks Hub"
ESC = bytes([escape.ESCAPE_KEY])
QUIT = ESC + b"q"


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
        return Endpoint("pipe", f"pb-repl-{unique}")
    return Endpoint("unix", str(tmp_path / f"{unique}.sock"))


@pytest.fixture
async def service(
    endpoint: Endpoint, hub: FakeHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncGenerator[FakeHub, None]:
    runtime = tmp_path / "runtime"
    # 0700, because the service refuses a runtime directory other users can
    # read -- its socket and its token live there. A test that made one the
    # usual way would be refused, but only on POSIX, where the check means
    # something.
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("PB_ENDPOINT", str(endpoint))
    async with daemon_module.run_daemon(
        endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
    ):
        yield hub


class _Screen:
    """A terminal that types from a script and keeps everything written to it.

    Keys are handed over one read at a time, which is how a real console
    behaves too, and only once :attr:`go` is set.
    """

    def __init__(self, *keys: bytes) -> None:
        """Queue up what to type.

        Args:
            *keys: One read each, delivered in order once released.
        """
        self.written = bytearray()
        self.typed: list[bytes] = list(keys)
        self.go = anyio.Event()
        self._size = (80, 24)

    def read(self) -> bytes:
        if not self.go.is_set() or not self.typed:
            return b""
        return self.typed.pop(0)

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def size(self) -> tuple[int, int]:
        return self._size

    @property
    def text(self) -> str:
        return self.written.decode("utf-8", "replace")


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_Screen]]:
    """Put a scripted terminal where the real one would be.

    Yields:
        A one-item list the test fills with the screen to use, so that the
        keystrokes can be chosen per test.
    """
    holder: list[_Screen] = []

    @contextlib.contextmanager
    def raw_console() -> Generator[_Screen]:
        yield holder[0]

    monkeypatch.setattr(console_module, "raw_console", raw_console)
    monkeypatch.setattr(console_module, "why_unavailable", lambda: None)
    # Nothing should wait a hundredth of a second for a scripted keystroke.
    monkeypatch.setattr(session, "KEY_POLL_SECONDS", 0)
    monkeypatch.setattr(session, "TICK_SECONDS", 0.01)
    yield holder


Prelude = Callable[[_Screen], Coroutine[Any, Any, None]]
Command = Callable[[], Coroutine[Any, Any, int]]


async def _run(
    screen: _Screen,
    holder: list[_Screen],
    prelude: Prelude | None = None,
    command: Command | None = None,
) -> int:
    """Connect, then run one session.

    Args:
        screen: The terminal to give the session.
        holder: Where the terminal fixture looks for it.
        prelude: Runs alongside the session and is responsible for releasing
            its keystrokes. Without one they are released immediately.
        command: Which command opens the session. Defaults to ``pb repl``.

    Returns:
        The command's exit code.
    """
    holder.append(screen)
    await hub_commands.connect_cmd(hub=HUB_NAME)
    code = errors.EXIT_ERROR

    async with anyio.create_task_group() as tg:
        if prelude is None:
            screen.go.set()
        else:
            tg.start_soon(prelude, screen)
        with anyio.fail_after(20):
            code = await (command or repl_command.repl_cmd)()
        tg.cancel_scope.cancel()

    return code


async def _when(condition: Callable[[], bool]) -> None:
    """Wait for something the hub is about to do.

    A poll rather than an event, because what is being waited for is a change
    in the simulated hub's own state, which has nothing to signal with. Ruff
    is told so: the alternative it suggests does not exist here.

    Args:
        condition: What has to become true.
    """
    with anyio.fail_after(5):
        while not condition():  # noqa: ASYNC110 - see above
            await anyio.sleep(0.005)


async def test_a_session_starts_the_interpreter_and_shows_what_it_says(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: hub.running_program_id == p.UserProgramId.REPL)
        hub.send_stdout(b">>> ")
        await _when(lambda: ">>> " in screen.text)
        screen.go.set()

    screen = _Screen(QUIT)
    assert await _run(screen, terminal, talk) == errors.EXIT_OK
    assert ">>> " in screen.text


async def test_the_escape_key_leaves_and_everything_else_goes_to_the_hub(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        screen.go.set()
        # Ctrl-C is the case that matters: it belongs to the hub, where it
        # interrupts, and must not be taken as an interrupt for us.
        await _when(lambda: bytes(hub.stdin) == b"print(1)\r\x03")

    screen = _Screen(b"print(1)\r", b"\x03", QUIT)
    assert await _run(screen, terminal, talk) == errors.EXIT_OK
    assert bytes(hub.stdin) == b"print(1)\r\x03"


async def test_the_escape_key_reaches_the_hub_when_pressed_twice(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        screen.go.set()
        await _when(lambda: bytes(hub.stdin) == ESC)

    assert await _run(_Screen(ESC + ESC, QUIT), terminal, talk) == errors.EXIT_OK
    assert bytes(hub.stdin) == ESC


async def test_a_command_split_across_reads_still_ends_the_session(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    # How it always arrives on a real console, where each key is its own read.
    assert await _run(_Screen(ESC, b"q"), terminal) == errors.EXIT_OK


async def test_the_status_line_says_which_hub_and_what_it_is_doing(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: hub.running_program_id == p.UserProgramId.REPL)
        await _when(lambda: "REPL" in screen.text)
        screen.go.set()

    screen = _Screen(QUIT)
    assert await _run(screen, terminal, talk) == errors.EXIT_OK
    assert HUB_NAME in screen.text
    # And the bottom row is handed back, margin and all.
    assert screen.text.endswith("\x1b[r\x1b[24;1H\x1b[2K")


async def test_a_battery_warning_appears_in_the_status_line(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        hub.status |= p.StatusFlag.BATTERY_LOW_VOLTAGE_WARNING
        hub.send_status()
        await _when(lambda: "battery low" in screen.text)
        screen.go.set()

    screen = _Screen(QUIT)
    assert await _run(screen, terminal, talk) == errors.EXIT_OK
    assert "battery low" in screen.text


async def test_the_help_lists_what_the_escape_key_can_do(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    screen = _Screen(ESC + b"?", QUIT)
    assert await _run(screen, terminal) == errors.EXIT_OK
    assert "leave the session" in screen.text
    assert "Ctrl-] Ctrl-]" in screen.text


async def test_an_unknown_command_says_so_rather_than_vanishing(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    screen = _Screen(ESC + b"z", QUIT)
    assert await _run(screen, terminal) == errors.EXIT_OK
    assert "no such command" in screen.text


async def test_stopping_from_the_session_stops_the_program(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: bool(hub.status & p.StatusFlag.USER_PROGRAM_RUNNING))
        screen.go.set()
        await _when(lambda: not hub.status & p.StatusFlag.USER_PROGRAM_RUNNING)

    assert await _run(_Screen(ESC + b"s", QUIT), terminal, talk) == errors.EXIT_OK
    assert not hub.status & p.StatusFlag.USER_PROGRAM_RUNNING


async def test_a_hub_that_disconnects_ends_the_session(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: hub.running_program_id == p.UserProgramId.REPL)
        hub.disconnect()

    screen = _Screen()
    # The hub going away is a hub error rather than a clean exit: a script
    # that ran this needs to hear about it.
    assert await _run(screen, terminal, talk) == errors.EXIT_HUB_ERROR
    # And it says which of the two things happened, since "somebody
    # disconnected it" and "it stopped answering" want different responses.
    assert "stopped answering" in screen.text
    # And it says what that leaves behind: the interpreter is still running on
    # a hub that can no longer be told to stop it, so nothing will find that
    # hub until somebody stops it at the hub. Said conditionally, because a hub
    # that has gone may have gone by being switched off already, and the fix
    # offered is the button rather than the power, because stopping the program
    # is all that is actually needed.
    assert "If the hub is still switched on" in screen.text
    assert "Press the hub's button" in screen.text


async def test_a_hub_disconnected_on_purpose_needs_no_rescuing(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: hub.running_program_id == p.UserProgramId.REPL)
        await hub_commands.disconnect_cmd(hub=HUB_NAME)

    screen = _Screen()
    # Disconnecting stops a built-in program on the way out, so what this
    # session sees first is its interpreter stopping, which is an ordinary end
    # rather than a failure. The hub is advertising again by then, and there
    # is nothing for anyone to go and do to it: saying otherwise would send
    # the user to fetch a hub that is fine.
    assert await _run(screen, terminal, talk) == errors.EXIT_OK
    assert not hub.status & p.StatusFlag.USER_PROGRAM_RUNNING
    assert "Press the hub's button" not in screen.text


async def test_leaving_stops_the_interpreter_it_started(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: hub.running_program_id == p.UserProgramId.REPL)
        screen.go.set()

    screen = _Screen(QUIT)
    assert await _run(screen, terminal, talk) == errors.EXIT_OK

    # A hub only advertises when nothing is running on it, so a session that
    # left the interpreter going would leave a hub nothing could find.
    assert not hub.status & p.StatusFlag.USER_PROGRAM_RUNNING
    assert "can be found again" in screen.text


async def test_an_interpreter_that_was_already_running_is_left_alone(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service
    hub.running_program_id = p.UserProgramId.REPL

    screen = _Screen(QUIT)
    holder = terminal
    holder.append(screen)
    await hub_commands.connect_cmd(hub=HUB_NAME)
    hub.set_program_running(True)
    await anyio.sleep(0.1)
    screen.go.set()

    with anyio.fail_after(10):
        assert await repl_command.repl_cmd() == errors.EXIT_OK

    # Somebody else's session, so it is not ours to end.
    assert hub.status & p.StatusFlag.USER_PROGRAM_RUNNING
    assert "Joining the interpreter" in screen.text


async def test_a_running_program_is_left_alone(
    service: FakeHub, terminal: list[_Screen], capsys: pytest.CaptureFixture[str]
) -> None:
    hub = service
    hub.program_size = 8
    hub.running_program_id = 0

    screen = _Screen(QUIT)
    holder = terminal
    holder.append(screen)
    await hub_commands.connect_cmd(hub=HUB_NAME)
    # Reported after connecting, since that is the only way the service could
    # have heard about it.
    hub.set_program_running(True)
    await anyio.sleep(0.1)
    screen.go.set()

    with anyio.fail_after(10):
        assert await repl_command.repl_cmd() == errors.EXIT_HUB_ERROR

    # The hub runs one thing at a time, so the interpreter could only start by
    # stopping the program somebody deliberately set going -- and nothing here
    # could hand the prompt back afterwards. So it is refused, and said in the
    # ordinary way rather than inside a session that never began.
    assert hub.running_program_id == 0
    assert hub.status & p.StatusFlag.USER_PROGRAM_RUNNING
    assert not screen.written
    assert "A program is running on the hub" in capsys.readouterr().err


async def test_the_interpreter_stopping_ends_the_session(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: hub.running_program_id == p.UserProgramId.REPL)
        # What Ctrl-D at the prompt does: the interpreter is a program as far
        # as the hub is concerned, and ending it leaves nothing to type at.
        hub.set_program_running(False)

    screen = _Screen()
    assert await _run(screen, terminal, talk) == errors.EXIT_OK
    assert "The interpreter stopped" in screen.text


async def test_input_is_cut_to_fit_a_hub_with_a_small_buffer(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service
    # Firmware from before the input buffer grew: 21 bytes, and a write that
    # does not fit whole is refused rather than partly kept.
    hub.stdin_capacity = 21
    typed = b"print('a line longer than twenty-one bytes')\r"

    async def talk(screen: _Screen) -> None:
        screen.go.set()
        # Stands for the program reading its input as it arrives, which is
        # what makes room for the next piece.
        while True:
            await anyio.sleep(0.005)
            hub.stdin_capacity = 21 + len(hub.stdin)

    screen = _Screen(typed, QUIT)
    assert await _run(screen, terminal, talk) == errors.EXIT_OK

    # Every byte arrived, in order, though no single write would have fitted.
    assert bytes(hub.stdin) == typed
    assert len(hub.stdin_writes) > 1
    assert max(len(write) for write in hub.stdin_writes) <= 21


# ------------------------------------------------- the same session, a program


def _script(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "demo.py"
    path.write_text(source, encoding="utf-8")
    return path


async def test_run_interactive_shows_what_the_program_prints(
    service: FakeHub, terminal: list[_Screen], tmp_path: Path
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: bool(hub.status & p.StatusFlag.USER_PROGRAM_RUNNING))
        hub.send_stdout(b"counting\r\n")
        await _when(lambda: "counting" in screen.text)
        # A program ends by itself, which ends the session with it.
        hub.set_program_running(False)

    screen = _Screen()

    async def command() -> int:
        return await program_commands.run_cmd(
            _script(tmp_path, "print('counting')\n"), hub=HUB_NAME
        )

    assert await _run(screen, terminal, talk, command) == errors.EXIT_OK
    assert "counting" in screen.text
    assert "The program stopped" in screen.text


TRACEBACK = (
    b"Traceback (most recent call last):\r\n"
    b'  File "__main__.py", line 3, in <module>\r\n'
    b"ZeroDivisionError: divide by zero\r\n"
)
"""What the firmware prints for a program that ended in an exception.

``print_final_exception`` in ``bricks/_common/micropython.c`` prints this for
an unhandled exception, "The program was stopped (SystemExit)." for a
deliberate stop, and nothing at all for a program that simply ended.
"""


async def test_a_program_that_ends_in_a_traceback_fails_the_command(
    service: FakeHub, terminal: list[_Screen], tmp_path: Path
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: bool(hub.status & p.StatusFlag.USER_PROGRAM_RUNNING))
        hub.send_stdout(TRACEBACK)
        await _when(lambda: "ZeroDivisionError" in screen.text)
        hub.set_program_running(False)

    screen = _Screen()

    async def command() -> int:
        return await program_commands.run_cmd(
            _script(tmp_path, "1 / 0\n"), hub=HUB_NAME
        )

    # Nothing in the protocol says a program failed rather than finished, so
    # what it printed on its way out is the only evidence there is -- and a
    # command that ran a program which died in front of the user has not
    # succeeded.
    assert await _run(screen, terminal, talk, command) == errors.EXIT_HUB_ERROR


async def test_a_program_that_prints_and_then_ends_well_does_not_fail(
    service: FakeHub, terminal: list[_Screen], tmp_path: Path
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: bool(hub.status & p.StatusFlag.USER_PROGRAM_RUNNING))
        # A traceback printed and recovered from, a long way from the end.
        hub.send_stdout(TRACEBACK)
        hub.send_stdout(b"carrying on\r\n" + b"x" * session.TAIL_BYTES)
        await _when(lambda: "carrying on" in screen.text)
        hub.set_program_running(False)

    screen = _Screen()

    async def command() -> int:
        return await program_commands.run_cmd(
            _script(tmp_path, "print('hi')\n"), hub=HUB_NAME
        )

    assert await _run(screen, terminal, talk, command) == errors.EXIT_OK


async def test_an_exception_at_the_interpreter_is_not_a_failure(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: hub.running_program_id == p.UserProgramId.REPL)
        # Typing something that raises is an ordinary thing to do at a prompt,
        # and the session it happened in ended perfectly well.
        hub.send_stdout(TRACEBACK)
        await _when(lambda: "ZeroDivisionError" in screen.text)
        screen.go.set()

    screen = _Screen(QUIT)
    assert await _run(screen, terminal, talk) == errors.EXIT_OK


async def test_leaving_an_interactive_run_stops_the_program(
    service: FakeHub, terminal: list[_Screen], tmp_path: Path
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        await _when(lambda: bool(hub.status & p.StatusFlag.USER_PROGRAM_RUNNING))
        screen.go.set()

    screen = _Screen(QUIT)

    async def command() -> int:
        return await program_commands.run_cmd(
            _script(tmp_path, "print('hi')\n"), hub=HUB_NAME
        )

    assert await _run(screen, terminal, talk, command) == errors.EXIT_OK
    # A hub only advertises when nothing is running on it, so a program this
    # command started is stopped on the way out, exactly as the interpreter is.
    assert not hub.status & p.StatusFlag.USER_PROGRAM_RUNNING


async def test_start_stays_with_what_is_already_on_the_hub(
    service: FakeHub, terminal: list[_Screen]
) -> None:
    hub = service
    # Stands for a program downloaded earlier, or put there by another tool.
    hub.program_size = 8

    async def talk(screen: _Screen) -> None:
        await _when(lambda: bool(hub.status & p.StatusFlag.USER_PROGRAM_RUNNING))
        hub.send_stdout(b"again\r\n")
        await _when(lambda: "again" in screen.text)
        hub.set_program_running(False)

    screen = _Screen()

    async def command() -> int:
        return await program_commands.start_cmd(hub=HUB_NAME)

    assert await _run(screen, terminal, talk, command) == errors.EXIT_OK
    assert "again" in screen.text


async def test_wait_by_name_leaves_the_terminal_alone(
    service: FakeHub, terminal: list[_Screen], tmp_path: Path
) -> None:
    hub = service

    async def talk(screen: _Screen) -> None:
        del screen
        await _when(lambda: bool(hub.status & p.StatusFlag.USER_PROGRAM_RUNNING))
        hub.send_stdout(b"counting\n")
        await anyio.sleep(0.05)
        hub.set_program_running(False)

    screen = _Screen()

    async def command() -> int:
        # --wait says stay, and says nothing about wanting the terminal taken
        # over, so it is taken to mean the plain form. That is the one thing a
        # script run from a terminal needs to be able to ask for.
        return await program_commands.run_cmd(
            _script(tmp_path, "print('counting')\n"), hub=HUB_NAME, wait=True
        )

    assert await _run(screen, terminal, talk, command) == errors.EXIT_OK
    # Nothing was drawn on the terminal at all: no status line, no margin.
    assert not screen.written
