# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""How long a keystroke takes to come back, through the whole of ``pb repl``.

The one measurement that cannot be taken against a simulated hub, because what
it measures is the Bluetooth round trip. A write to a hub is a GATT write with
response, so it costs a connection event -- about 70 ms on a City hub -- and
sending one keystroke per write meant every key waited behind every key before
it. The delay then did not settle at one round trip; it *grew* for as long as
the typing went on, which is why it was reported as "sometimes a very long
delay" rather than as a constant lag.

Measured before the fix, a 21-character line: 79 ms for the first character's
echo and 1479 ms for the last, rising about 70 ms each. After: 108 ms to
228 ms, flat.

So the assertion is about the *shape* rather than the size. A round trip is
whatever the radio and the host's Bluetooth stack make it, and the host does
not get told which connection interval was negotiated -- but a backlog that
builds shows up as the last echo being far worse than the first, whatever the
interval is.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Generator, Iterator

import anyio
import pytest

from pb_cli import console as console_module
from pb_cli import errors, escape, session
from pb_cli.commands import repl as repl_command

pytestmark = [pytest.mark.hardware, pytest.mark.anyio]

QUIT = bytes([escape.ESCAPE_KEY]) + b"q"

TYPED = b"print('the quick brown fox')"
"""What is typed. Long enough that a backlog has room to build, and harmless:
it is never submitted, since the session leaves before any Enter."""

SPREAD_LIMIT_MS = 500.0
"""How much worse the slowest echo may be than the quickest.

A backlog shows up here and nothing else does: the round trip itself cancels
out. Measured at about 150 ms of spread when the whole backlog goes in one
write, and 1400 ms when each keystroke went on its own.
"""

WORST_LIMIT_MS = 900.0
"""How long any one character may take to come back.

Belt as well as braces, for the case where every echo is uniformly late rather
than progressively later. Measured at 240 ms at worst; 1479 ms before the fix.
"""


class _Typist:
    """A terminal that types a line and waits to see all of it echoed back.

    Keys are handed over one read at a time, as a real console does, and the
    time of each is recorded. Reading stops when every character has come back
    or the patience runs out, at which point it types the keys that leave.
    """

    def __init__(self, line: bytes) -> None:
        """Queue up the line to type.

        Args:
            line: The characters, each of which becomes one read.
        """
        self.written = bytearray()
        self.pressed: list[float] = []
        self.echoed: list[float] = []
        self._to_type = [bytes([byte]) for byte in line]
        self._wanted = len(line)
        self._leaving = False
        self.go = anyio.Event()

    async def wait(self) -> None:
        await self.go.wait()

        if not self._to_type:
            # Nothing left to type and not everything is back yet, so sit
            # here as a real terminal would while its user waits.
            await anyio.sleep(0.005)

    def read(self) -> bytes:
        if not self.go.is_set():
            return b""

        if self._to_type:
            self.pressed.append(time.monotonic())

            return self._to_type.pop(0)

        if len(self.echoed) >= self._wanted and not self._leaving:
            self._leaving = True

            return QUIT

        return b""

    def write(self, data: bytes) -> None:
        self.written.extend(data)
        now = time.monotonic()
        # Only the characters count. The hub also sends its prompt, and the
        # status line is drawn with control sequences of our own.
        self.echoed.extend(now for byte in data if 0x20 <= byte < 0x7F)

    def size(self) -> tuple[int, int]:
        return (80, 24)

    @property
    def text(self) -> str:
        """Everything written to the screen, for a failure message."""
        return self.written.decode("utf-8", "replace")

    @property
    def delays_ms(self) -> list[float]:
        """One latency per character that came back, in milliseconds."""
        return [
            round((back - out) * 1000, 1)
            for out, back in zip(self.pressed, self.echoed, strict=False)
        ]


@pytest.fixture
def typist() -> Iterator[list[_Typist]]:
    """Put a typist where the real terminal would be.

    Yields:
        A one-item list the test fills with the terminal to use.
    """
    holder: list[_Typist] = []

    @contextlib.contextmanager
    def raw_console() -> Generator[_Typist]:
        yield holder[0]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(console_module, "raw_console", raw_console)
        patch.setattr(console_module, "why_unavailable", lambda: None)
        yield holder


@pytest.mark.timeout(120)
async def test_typing_at_the_interpreter_does_not_fall_further_behind(
    hub: str, typist: list[_Typist]
) -> None:
    screen = _Typist(TYPED)
    typist.append(screen)
    # Bound before the block: a task group may swallow what its body raised,
    # so nothing assigned inside one is certain to exist after it.
    code = errors.EXIT_ERROR

    async def release() -> None:
        # After the prompt has arrived, so that the first character measured is
        # a character and not the tail of the banner.
        await anyio.sleep(1.5)
        screen.echoed.clear()
        screen.go.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(release)

        with anyio.fail_after(90):
            code = await repl_command.repl_cmd(hub=hub)

        tg.cancel_scope.cancel()

    assert code == errors.EXIT_OK
    delays = screen.delays_ms
    assert len(delays) >= len(TYPED) - 2, (
        f"only {len(delays)} of {len(TYPED)} characters came back: {screen.text!r}"
    )

    spread = max(delays) - min(delays)
    assert spread < SPREAD_LIMIT_MS, (
        f"the echo fell further behind as the line went on -- {delays[0]:.0f} ms "
        f"for the first character and {delays[-1]:.0f} ms for the last, a spread "
        f"of {spread:.0f} ms. Each keystroke is costing its own round trip "
        f"instead of the backlog going in one write. {delays}"
    )
    assert max(delays) < WORST_LIMIT_MS, f"slowest echo {max(delays):.0f} ms: {delays}"


def test_the_session_module_is_the_one_under_test() -> None:
    # The measurement above is only worth anything if it went through the pump
    # that ships. Named here so that moving it does not quietly turn this file
    # into a test of nothing.
    assert hasattr(session, "_send_input")
