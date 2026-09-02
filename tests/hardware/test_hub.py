# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The premise of the tool, against a hub that is actually switched on.

Everything here is also covered against the simulated hub, and that coverage is
what makes these few worth having rather than dozens: they are the ones where a
fake could be wrong about the thing it stands in for. This repository has been
caught by that three times -- an arrow key on Windows, the same arrow key on
POSIX, and a write size that a full fake buffer made look correct -- so what is
tested here is chosen for being unfalsifiable any other way.

Timings are asserted loosely. A Bluetooth link is shared with whatever else the
machine's radio is doing, so the numbers move; the thresholds are set where a
regression is an order of magnitude away rather than where today's measurement
sits.
"""

from __future__ import annotations

import contextlib
import time

import anyio
import pytest

import pb_client

pytestmark = [pytest.mark.hardware, pytest.mark.anyio]

_REFUSED = (pb_client.JsonRpcError, TimeoutError, BaseExceptionGroup)
"""What a hub that will not take input raises, once the service gives up.

The group is not optional: anything raised inside an ``async with
pb_client.connect()`` comes back wrapped in a group of one.
"""

PRINTS = "print('hello from a real hub')\n"
"""A program with nothing to it, so that what is tested is the round trip."""

DIVIDES_BY_ZERO = "print('about to fail')\n1 / 0\n"
"""A program that ends in an exception, which the hub reports only by printing.

Nothing in the protocol says whether a program finished or failed, so the exit
code is read out of the traceback. That heuristic is the reason this test is
here: the wording belongs to the firmware and can change without anything in
this repository noticing.
"""


async def _run_and_collect(
    service: pb_client.Service, hub: str, source: str, *, seconds: float = 15.0
) -> str:
    """Run a program and return everything it printed.

    Args:
        service: The running service.
        hub: Which hub.
        source: The program.
        seconds: How long to wait for it to finish.

    Returns:
        Its output, decoded.
    """
    program = await pb_client.compile_source(source)
    seen = bytearray()

    async with service.attach(hub_id=hub, replay_bytes=0) as events:
        await service.download(program, hub_id=hub)
        await service.start_program(hub_id=hub)
        started = False

        with anyio.move_on_after(seconds):
            async for event in events:
                if isinstance(event, pb_client.Output):
                    seen += event.data

                elif isinstance(event, pb_client.Status):
                    if event.program_running:
                        started = True

                    elif started:
                        # It ran and has stopped, so everything it was going
                        # to print has been printed.
                        break

    return seen.decode("utf-8", "replace")


async def test_a_program_runs_and_says_what_it_printed(
    service: pb_client.Service, hub: str
) -> None:
    assert "hello from a real hub" in await _run_and_collect(service, hub, PRINTS)


async def test_the_hub_still_reports_the_traceback_this_tool_reads(
    service: pb_client.Service, hub: str
) -> None:
    # How a program ended has to be read out of what it printed, so the two
    # strings that reading depends on are checked against a real hub. If the
    # firmware ever rewords print_final_exception, this is what notices.
    printed = await _run_and_collect(service, hub, DIVIDES_BY_ZERO)

    assert "about to fail" in printed
    assert "Traceback (most recent call last):" in printed
    assert "ZeroDivisionError" in printed


async def test_the_connection_outlives_the_client_that_made_it(
    service: pb_client.Service, hub: str
) -> None:
    # The whole premise: state lives in the service, so a second client sees a
    # hub it did not connect to, and the first one leaving changes nothing.
    async with pb_client.connect(autostart=False) as second:
        hubs, _ = await second.list_hubs()

    assert hub in [info.name for info in hubs]

    # And again after that client has gone, which is the case a user meets as
    # one command after another.
    async with pb_client.connect(autostart=False) as third:
        info = await third.hub_info(hub)

    assert info.name == hub
    assert info.state == "connected"


async def test_input_reaches_the_interpreter_and_is_echoed(
    service: pb_client.Service, hub: str
) -> None:
    # The plainest possible check that stdin works at all, before the two
    # timing tests below ask how well.
    async with service.attach(hub_id=hub, replay_bytes=0) as events:
        await service.start_repl(hub_id=hub)
        await anyio.sleep(1.0)
        await service.write_stdin(b"print(6 * 7)\r", hub_id=hub)
        seen = bytearray()

        with anyio.move_on_after(10.0):
            async for event in events:
                if isinstance(event, pb_client.Output):
                    seen += event.data

                    if b"42" in seen:
                        break

    assert b"42" in seen, seen.decode("utf-8", "replace")


PASTE = b"# " + b"filler " * 60 + b"\r"
"""About four hundred bytes: one write on a hub that will take it, and twenty
on a hub whose write size has been walked down to the old floor."""

IGNORES_INPUT = """
from pybricks.tools import wait

for _ in range(20):
    wait(100)
"""
"""A program that never reads sys.stdin, so nothing drains the input buffer.

This is the case that used to be mistaken for firmware with a small buffer.
"""

PASTE_LIMIT_MS = 800.0
"""How long a four-hundred-byte paste may take.

Measured at 71 to 94 ms on a City hub, and 2589 ms when the write size had been
walked down to twenty bytes. Set between the two, nearer the failure, so that
the radio having a bad afternoon does not fail the build.
"""


async def _paste_ms(service: pb_client.Service, hub: str) -> float:
    """Time one block of input into the interpreter, in milliseconds."""
    began = time.monotonic()
    await service.write_stdin(PASTE, hub_id=hub)

    return (time.monotonic() - began) * 1000


@pytest.mark.timeout(120)
async def test_a_program_that_ignores_input_does_not_slow_the_hub_afterwards(
    service: pb_client.Service, hub: str
) -> None:
    # The write size must not be inferred from a refusal. A refusal means
    # nothing on the hub is reading -- which a program that ignores sys.stdin
    # does for its whole run -- and an earlier version read it as proof that
    # the buffer was smaller than advertised, halved the write size, and
    # remembered that for the life of the connection. The connection outlives
    # every command, so pasting stayed slow for the rest of the day.
    await service.start_repl(hub_id=hub)
    await anyio.sleep(1.0)
    before = await _paste_ms(service, hub)
    assert before < PASTE_LIMIT_MS, f"a fresh connection was already slow: {before} ms"

    # The interpreter has to go first: a hub runs one thing at a time, so one
    # that is busy with the REPL will not store a program.
    await service.stop_program(hub_id=hub)
    await anyio.sleep(0.5)

    program = await pb_client.compile_source(IGNORES_INPUT)
    await service.download(program, hub_id=hub)
    await service.start_program(hub_id=hub)

    # The first write fits an empty buffer even though nothing will read it,
    # so it is the one after it that meets a buffer with something in it and
    # is refused. That refusal lasts as long as the program does.
    await service.write_stdin(PASTE, hub_id=hub)

    began = time.monotonic()

    with contextlib.suppress(*_REFUSED):
        with anyio.move_on_after(30.0):
            await service.write_stdin(PASTE, hub_id=hub)

    refused_for = time.monotonic() - began

    if refused_for <= 2.0:
        # Not a failure. The damage this guards against was done by a
        # *sustained* refusal, so a hub that never refused the input has
        # nothing to have provoked it and nothing to measure the recovery
        # from. An EV3 reached over USB is like that: it took the same
        # four-hundred-byte write immediately, with the same program ignoring
        # its input. The guard earns its keep on a City hub over Bluetooth,
        # which is where it was checked by mutation.
        pytest.skip(
            f"this hub took the input after {refused_for:.1f} s, so no "
            f"sustained refusal could be provoked to measure"
        )

    await service.stop_program(hub_id=hub)
    await anyio.sleep(0.5)
    await service.start_repl(hub_id=hub)
    await anyio.sleep(1.0)

    after = await _paste_ms(service, hub)
    assert after < PASTE_LIMIT_MS, (
        f"pasting took {after:.0f} ms after a program that ignored its input, "
        f"against {before:.0f} ms before it: something has concluded the hub "
        f"takes less input than it said, and nothing puts that back"
    )
