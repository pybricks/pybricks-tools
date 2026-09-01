# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Compiling, downloading and running programs, end to end."""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import anyio
import pytest

import pb_client
import pb_service.methods.hub  # pyright: ignore[reportUnusedImport]
import pb_service.methods.program  # pyright: ignore[reportUnusedImport]

# Imported to register the RPC methods.
import pb_service.methods.service  # pyright: ignore[reportUnusedImport]
import pb_service.methods.stdio  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pb_core.ipc import Endpoint
from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_service import service as service_module
from pb_service.hub import protocol as p
from pb_service.hub.fake import FakeHub, FakeHubTransport

pytestmark = pytest.mark.anyio

HUB_NAME = "Pybricks Hub"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


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
    yield


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub(name=HUB_NAME, address="00:11:22:33:44:55")


def _endpoint(tmp_path: Path) -> Endpoint:
    """A socket or pipe of this test's own.

    Args:
        tmp_path: Somewhere to put a socket, on the platforms that need one.

    Returns:
        Where a service for one test should listen.
    """
    unique = uuid.uuid4().hex[:12]

    if sys.platform == "win32":
        return Endpoint("pipe", f"pb-prog-{unique}")

    return Endpoint("unix", str(tmp_path / f"{unique}.sock"))


@pytest.fixture
async def connected(tmp_path: Path, hub: FakeHub) -> AsyncGenerator[Endpoint, None]:
    endpoint = _endpoint(tmp_path)

    async with service_module.run_service(
        endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
    ):
        async with pb_client.connect(endpoint, autostart=False) as service:
            await service.connect_hub(name=HUB_NAME)

        yield endpoint


def _script(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "demo.py"
    path.write_text(source, encoding="utf-8")
    return path


# ------------------------------------------------------------------ compiling


async def test_compiles_a_script_to_the_container_the_hub_reads(
    tmp_path: Path,
) -> None:
    # Not a bare .mpy: the firmware reads a stored program as a run of
    # (size, name, mpy) records and checks the MicroPython magic only after
    # stepping over that header, so a bare .mpy is read as a garbage header
    # and refused.
    program = await pb_client.compile_script(_script(tmp_path, "print('hi')\n"))
    size = int.from_bytes(program[:4], "little")
    name, _, mpy = program[4:].partition(b"\x00")
    # Named after its file rather than "__main__". Firmware from Pybricks
    # profile v1.5.0 runs whichever module comes first whatever it is called,
    # and shows that name as the program's -- so "__main__" would put
    # "__main__" on the hub.
    assert name == b"demo"
    assert len(mpy) == size
    assert mpy[:1] == b"M"


async def test_an_older_hub_gets_the_legacy_main_module(tmp_path: Path) -> None:
    # Before profile v1.5.0 the firmware looked the entry module up by the
    # name __main__, so a hub that old needs it called that or nothing runs.
    compiled = await pb_client.compile_program(_script(tmp_path, "print('hi')\n"))
    name, _, _ = compiled.pack(legacy_main=True)[4:].partition(b"\x00")
    assert name == b"__main__"


async def test_a_program_of_several_files(tmp_path: Path) -> None:
    (tmp_path / "helpers.py").write_text("SPEED = 100\n", encoding="utf-8")
    (tmp_path / "robot").mkdir()
    (tmp_path / "robot" / "motors.py").write_text("import helpers\n", encoding="utf-8")
    script = _script(tmp_path, "from robot.motors import helpers\nimport helpers\n")

    compiled = await pb_client.compile_program(script, roots=[tmp_path])

    # Entry first, because position is what the firmware uses to choose the
    # program to run. A dotted name is one flat module name to the hub, and
    # robot/ needs no __init__.py, because the hub has no notion of a package.
    assert [module.name for module in compiled.modules] == [
        "demo",
        "robot.motors",
        "helpers",
    ]
    packed = compiled.pack()

    for module in compiled.modules:
        assert module.name.encode() + b"\x00" in packed


async def test_compiles_source_with_no_file_behind_it() -> None:
    # What "pb run -c" compiles. The container is the same as for a file: the
    # hub has no notion of where the source came from.
    program = await pb_client.compile_source("print('hi')\n")
    name, _, mpy = program[4:].partition(b"\x00")
    assert name == b"__main__"
    assert mpy[:1] == b"M"


async def test_source_with_no_file_is_called_string_on_the_hub() -> None:
    # The compiled module carries the name the hub prints in a traceback, and
    # there is no file to name. <string> is what CPython prints for python -c,
    # so a traceback from the hub reads the way a Python user already expects.
    program = await pb_client.compile_source("raise ValueError\n")
    assert b"<string>" in program


async def test_compile_error_carries_the_compiler_output(tmp_path: Path) -> None:
    # The compiler names the offending line, which is the only useful part of
    # a syntax error, so it must not be swallowed.
    with pytest.raises(pb_client.CompileError) as info:
        await pb_client.compile_script(_script(tmp_path, "def broken(\n"))

    assert "SyntaxError" in info.value.output


async def test_missing_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(pb_client.CompileError, match="could not read"):
        await pb_client.compile_script(tmp_path / "absent.py")


def test_compiler_description_names_its_architecture() -> None:
    # Logged at startup because the published Windows ARM64 wheel currently
    # ships an x86-64 binary, which runs under emulation.
    described = pb_client.describe_compiler()
    assert "mpy-cross" in described
    assert any(tag in described for tag in ("x86-64", "arm64", "x86", "native"))


# ---------------------------------------------------------------- downloading


async def test_download_puts_the_program_on_the_hub(
    connected: Endpoint, hub: FakeHub, tmp_path: Path
) -> None:
    program = await pb_client.compile_script(_script(tmp_path, "print('hi')\n"))

    async with pb_client.connect(connected, autostart=False) as service:
        written = await service.download(program)

    assert written.size == len(program)
    assert bytes(hub.program[: len(program)]) == program
    assert hub.program_size == len(program)
    # Where it went is part of the answer: the hub chooses the slot, so a
    # caller that did not ask still has to be told which one it was.
    assert written.slot == hub.selected_slot
    assert written.slots == hub.num_slots


async def test_a_download_goes_to_the_slot_the_hub_has_selected(
    connected: Endpoint, hub: FakeHub, tmp_path: Path
) -> None:
    hub.selected_slot = 2
    hub.send_status()
    program = await pb_client.compile_script(_script(tmp_path, "print('hi')\n"))

    async with pb_client.connect(connected, autostart=False) as service:
        # Naming the slot the hub has selected is agreement rather than an
        # instruction, and is carried out.
        written = await service.download(program, slot=2)
        assert written.slot == 2

        # Naming another one cannot be carried out at all: nothing in the
        # protocol selects a slot, so the program would land in slot 2 anyway
        # and a later start of slot 4 would run whatever was already there.
        with pytest.raises(JsonRpcError) as info:
            await service.download(program, slot=4)

    assert info.value.code == ErrorCode.PROGRAM_SLOT_NOT_SELECTED
    assert "slot 2 selected" in str(info.value)


async def test_a_single_slot_hub_says_so_rather_than_naming_a_slot(
    tmp_path: Path,
) -> None:
    # How many slots a hub has is read once, when it is connected to, so this
    # one has to be a single-slot hub from the start -- which is what a City,
    # Technic or Move hub is.
    hub = FakeHub(name=HUB_NAME, address="00:11:22:33:44:55", num_slots=1)
    program = await pb_client.compile_script(_script(tmp_path, "print('hi')\n"))
    endpoint = _endpoint(tmp_path)

    async with (
        service_module.run_service(
            endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
        ),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        await service.connect_hub(name=HUB_NAME)

        with pytest.raises(JsonRpcError, match="one program at a time"):
            await service.download(program, slot=3)


async def test_download_clears_the_metadata_first(
    connected: Endpoint, hub: FakeHub, tmp_path: Path
) -> None:
    # Zero first, real length last: an interrupted download must never leave
    # something that looks runnable.
    program = await pb_client.compile_script(_script(tmp_path, "print('hi')\n"))

    async with pb_client.connect(connected, autostart=False) as service:
        await service.download(program)

    metas = [
        command
        for command in hub.commands
        if command[0] == p.Command.WRITE_USER_PROGRAM_META
    ]
    assert len(metas) >= 2
    assert metas[0][1:5] == (0).to_bytes(4, "little")
    assert metas[-1][1:5] == len(program).to_bytes(4, "little")


async def test_a_program_that_is_too_large_is_refused(
    tmp_path: Path, hub: FakeHub
) -> None:
    hub.max_program_size = 32
    unique = uuid.uuid4().hex[:12]
    endpoint = (
        Endpoint("pipe", f"pb-big-{unique}")
        if sys.platform == "win32"
        else Endpoint("unix", str(tmp_path / f"{unique}.sock"))
    )

    async with (
        service_module.run_service(
            endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
        ),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        await service.connect_hub(name=HUB_NAME)
        program = await pb_client.compile_script(
            _script(tmp_path, "print('a longer program')\n" * 20)
        )

        with pytest.raises(JsonRpcError) as info:
            await service.download(program)

    assert info.value.code == ErrorCode.PROGRAM_TOO_LARGE
    # The limit is reported, so the message can say what would fit.
    assert info.value.data["limit"] == 32


async def test_download_is_chunked_to_the_hub_limit(
    tmp_path: Path, hub: FakeHub
) -> None:
    hub.max_write_size = 20
    unique = uuid.uuid4().hex[:12]
    endpoint = (
        Endpoint("pipe", f"pb-chunk-{unique}")
        if sys.platform == "win32"
        else Endpoint("unix", str(tmp_path / f"{unique}.sock"))
    )

    async with (
        service_module.run_service(
            endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
        ),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        await service.connect_hub(name=HUB_NAME)
        program = await pb_client.compile_script(_script(tmp_path, "print('hello')\n"))
        await service.download(program)

    writes = [c for c in hub.commands if c[0] == p.Command.WRITE_USER_RAM]
    assert len(writes) > 1, "a small write limit should force several chunks"

    for write in writes:
        assert len(write) <= hub.max_write_size

    assert bytes(hub.program[: len(program)]) == program


# ------------------------------------------------------------------- running


async def test_run_without_waiting_starts_the_program(
    connected: Endpoint, hub: FakeHub, tmp_path: Path
) -> None:
    program = await pb_client.compile_script(_script(tmp_path, "print('hi')\n"))

    async with pb_client.connect(connected, autostart=False) as service:
        reason = await service.run_program(program, wait=False)

    assert reason == "started"
    assert hub.status & p.StatusFlag.USER_PROGRAM_RUNNING


async def test_run_waits_for_the_program_to_finish(
    connected: Endpoint, hub: FakeHub, tmp_path: Path
) -> None:
    program = await pb_client.compile_script(_script(tmp_path, "print('hi')\n"))
    reasons: list[str] = []
    running = anyio.Event()

    def note_status() -> None:
        # Wait on a signal rather than polling the hub's state.
        if hub.status & p.StatusFlag.USER_PROGRAM_RUNNING:
            running.set()

    hub.on_status = note_status

    async with (
        pb_client.connect(connected, autostart=False) as runner,
        pb_client.connect(connected, autostart=False) as other,
    ):

        async def run() -> None:
            reasons.append(await runner.run_program(program, wait=True))

        async with anyio.create_task_group() as tg:
            tg.start_soon(run)

            # Let the program start, then stop it the way a second command
            # or the hub's own button would.
            with anyio.fail_after(5):
                await running.wait()

            await other.stop_program()

    assert reasons == ["stopped"]


async def test_start_uses_the_requested_slot(
    connected: Endpoint, hub: FakeHub, tmp_path: Path
) -> None:
    program = await pb_client.compile_script(_script(tmp_path, "print('hi')\n"))

    async with pb_client.connect(connected, autostart=False) as service:
        await service.download(program)
        await service.start_program(slot=2)

    starts = [c for c in hub.commands if c[0] == p.Command.START_USER_PROGRAM]
    assert starts[-1] == bytes([p.Command.START_USER_PROGRAM, 2])


async def test_starting_a_slot_on_old_firmware_is_refused(
    tmp_path: Path,
) -> None:
    # Silently running a different program than the one asked for would be
    # worse than failing.
    hub = FakeHub(name=HUB_NAME, address="00:11:22:33:44:55", profile=(1, 3, 0))
    unique = uuid.uuid4().hex[:12]
    endpoint = (
        Endpoint("pipe", f"pb-old-{unique}")
        if sys.platform == "win32"
        else Endpoint("unix", str(tmp_path / f"{unique}.sock"))
    )

    async with (
        service_module.run_service(
            endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
        ),
        pb_client.connect(endpoint, autostart=False) as service,
    ):
        await service.connect_hub(name=HUB_NAME)

        with pytest.raises(JsonRpcError) as info:
            await service.start_program(slot=2)

    assert info.value.code == ErrorCode.HUB_UNSUPPORTED


async def test_a_hub_that_refuses_says_so(connected: Endpoint, hub: FakeHub) -> None:
    # A refusal is an ordinary answer -- a busy hub, a slot with nothing in
    # it -- and must not arrive as an internal error with the reason left in
    # the service's log.
    hub.reject_next = p.HubError.BUSY

    async with pb_client.connect(connected, autostart=False) as service:
        with pytest.raises(JsonRpcError) as info:
            await service.start_program()

    assert info.value.code == ErrorCode.HUB_BUSY
    assert "would not start" in info.value.message
    # The firmware's own code travels with it, so a client can be exact.
    assert info.value.data["hub_error"] == int(p.HubError.BUSY)
    assert info.value.data["hub_error_name"] == "BUSY"


async def test_starting_names_the_program_on_new_firmware(
    connected: Endpoint, hub: FakeHub, tmp_path: Path
) -> None:
    # The no-argument form of START_USER_PROGRAM was removed in profile
    # v1.4.0, and a hub that speaks it refuses the legacy command outright, so
    # "whichever is selected" has to be resolved to a real slot before asking.
    async with pb_client.connect(connected, autostart=False) as service:
        program = await pb_client.compile_script(_script(tmp_path, "x = 1\n"))
        await service.download(program)
        await service.start_program()

    starts = [c for c in hub.commands if c[0] == p.Command.START_USER_PROGRAM]
    assert starts[-1] == bytes([p.Command.START_USER_PROGRAM, 0])
