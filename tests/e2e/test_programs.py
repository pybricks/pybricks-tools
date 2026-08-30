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
from pb_core.ipc import Endpoint
from pb_core.jsonrpc import ErrorCode, JsonRpcError
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


@pytest.fixture(autouse=True)
def _private_runtime(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("PB_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("PB_ENDPOINT", raising=False)
    yield


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub(name=HUB_NAME, address="00:11:22:33:44:55")


@pytest.fixture
async def connected(tmp_path: Path, hub: FakeHub) -> AsyncGenerator[Endpoint, None]:
    unique = uuid.uuid4().hex[:12]
    endpoint = (
        Endpoint("pipe", f"pb-prog-{unique}")
        if sys.platform == "win32"
        else Endpoint("unix", str(tmp_path / f"{unique}.sock"))
    )
    async with daemon_module.run_daemon(
        endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
    ):
        async with pb_client.connect(endpoint, autostart=False) as daemon:
            await daemon.connect_hub(name=HUB_NAME)
        yield endpoint


def _script(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "demo.py"
    path.write_text(source, encoding="utf-8")
    return path


# ------------------------------------------------------------------ compiling


async def test_compiles_a_script_to_mpy(tmp_path: Path) -> None:
    program = await pb_client.compile_script(_script(tmp_path, "print('hi')\n"))
    # The MicroPython bytecode container starts with 'M'.
    assert program[:1] == b"M"
    assert len(program) > 4


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
    async with pb_client.connect(connected, autostart=False) as daemon:
        size = await daemon.download(program)

    assert size == len(program)
    assert bytes(hub.program[: len(program)]) == program
    assert hub.program_size == len(program)


async def test_download_clears_the_metadata_first(
    connected: Endpoint, hub: FakeHub, tmp_path: Path
) -> None:
    # Zero first, real length last: an interrupted download must never leave
    # something that looks runnable.
    program = await pb_client.compile_script(_script(tmp_path, "print('hi')\n"))
    async with pb_client.connect(connected, autostart=False) as daemon:
        await daemon.download(program)

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
        daemon_module.run_daemon(
            endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
        ),
        pb_client.connect(endpoint, autostart=False) as daemon,
    ):
        await daemon.connect_hub(name=HUB_NAME)
        program = await pb_client.compile_script(
            _script(tmp_path, "print('a longer program')\n" * 20)
        )
        with pytest.raises(JsonRpcError) as info:
            await daemon.download(program)

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
        daemon_module.run_daemon(
            endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
        ),
        pb_client.connect(endpoint, autostart=False) as daemon,
    ):
        await daemon.connect_hub(name=HUB_NAME)
        program = await pb_client.compile_script(_script(tmp_path, "print('hello')\n"))
        await daemon.download(program)

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
    async with pb_client.connect(connected, autostart=False) as daemon:
        reason = await daemon.run_program(program, wait=False)
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
    async with pb_client.connect(connected, autostart=False) as daemon:
        await daemon.download(program)
        await daemon.start_program(slot=2)

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
        daemon_module.run_daemon(
            endpoint, idle_timeout=0, transport=FakeHubTransport(hub)
        ),
        pb_client.connect(endpoint, autostart=False) as daemon,
    ):
        await daemon.connect_hub(name=HUB_NAME)
        with pytest.raises(JsonRpcError) as info:
            await daemon.start_program(slot=2)

    assert info.value.code == ErrorCode.HUB_UNSUPPORTED
