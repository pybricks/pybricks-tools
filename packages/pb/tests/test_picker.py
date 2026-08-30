# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Choosing a hub from a list that is still being written."""

from __future__ import annotations

import contextlib
import io
from collections.abc import AsyncIterator, Iterator

import anyio
import pytest
from rich.console import Console, RenderableType

import pb_client
from pb import keys, picker
from pb.keys import Key

pytestmark = pytest.mark.anyio

HUBS = [
    pb_client.DeviceInfo(
        id=f"fake:{index}",
        name=name,
        address=f"00:00:00:00:00:0{index}",
        rssi=-50,
        transport="fake",
        hub_type="City hub",
    )
    for index, name in enumerate(["First Hub", "Second Hub", "Third Hub"])
]


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_terminal_fiddling(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    # Nothing here has a terminal, and on POSIX asking for cbreak mode without
    # one raises. The tests drive the keys directly instead.
    monkeypatch.setattr(keys, "reading_keys", contextlib.nullcontext)
    # No waiting between polls: the scripted keys are ready immediately.
    monkeypatch.setattr(picker, "POLL_SECONDS", 0)
    yield


def _console() -> Console:
    """A console that renders into nothing anyone will read."""
    return Console(file=io.StringIO())


def _render(devices: list[pb_client.DeviceInfo], selected: int) -> RenderableType:
    return f"{len(devices)} hubs, on {selected}"


class _Keyboard:
    """A scripted keyboard that stays quiet until the hubs have arrived.

    Without the gate the picker would consume the whole script before the
    first hub was listed, and pressing Enter on an empty list does nothing --
    so the test would hang rather than fail, which is the worst outcome.
    """

    def __init__(self, *script: Key | type[BaseException] | anyio.Event) -> None:
        """Queue up what to press.

        Args:
            *script: Keys in order. A ``KeyboardInterrupt`` class is raised
                instead of returned, and an event pauses the script until
                something sets it, which is how a test says "not until that
                has happened".
        """
        self.ready = anyio.Event()
        self._script: list[Key | type[BaseException] | anyio.Event] = list(script)

    def __call__(self) -> Key | None:
        if not self.ready.is_set():
            return None
        while self._script:
            head = self._script[0]
            if isinstance(head, anyio.Event):
                if not head.is_set():
                    return None
                self._script.pop(0)
                continue
            self._script.pop(0)
            if isinstance(head, type):
                raise head
            return head
        return None


async def _devices(
    keyboard: _Keyboard, hubs: list[pb_client.DeviceInfo]
) -> AsyncIterator[pb_client.DeviceInfo]:
    """Report hubs, then keep the scan open the way a real one does."""
    for hub in hubs:
        yield hub
    keyboard.ready.set()
    await anyio.sleep_forever()


async def _choose(
    monkeypatch: pytest.MonkeyPatch,
    keyboard: _Keyboard,
    hubs: list[pb_client.DeviceInfo] | None = None,
) -> pb_client.DeviceInfo | None:
    monkeypatch.setattr(keys, "poll_key", keyboard)
    with anyio.fail_after(5):
        return await picker.choose_hub(
            _console(), _devices(keyboard, hubs or HUBS), _render
        )


async def test_enter_takes_the_hub_at_the_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen = await _choose(monkeypatch, _Keyboard(Key.ENTER))
    assert chosen is not None
    assert chosen.name == "First Hub"


async def test_the_arrows_move_the_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen = await _choose(monkeypatch, _Keyboard(Key.DOWN, Key.DOWN, Key.ENTER))
    assert chosen is not None
    assert chosen.name == "Third Hub"


async def test_going_up_from_the_top_wraps_around(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A list of two with the highlight stuck at one end is a worse surprise
    # than wrapping.
    chosen = await _choose(monkeypatch, _Keyboard(Key.UP, Key.ENTER))
    assert chosen is not None
    assert chosen.name == "Third Hub"


async def test_leaving_without_choosing_is_an_ordinary_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _choose(monkeypatch, _Keyboard(Key.CANCEL)) is None


async def test_interrupting_is_not_an_error_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ctrl-C reaches this as KeyboardInterrupt rather than as a key, and
    # giving up on finding a hub is not a failure.
    assert await _choose(monkeypatch, _Keyboard(KeyboardInterrupt)) is None


async def test_a_key_that_means_nothing_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen = await _choose(monkeypatch, _Keyboard(Key.OTHER, Key.DOWN, Key.ENTER))
    assert chosen is not None
    assert chosen.name == "Second Hub"


async def test_a_hub_arriving_later_does_not_move_the_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hubs are appended as they are heard, so the highlight stays on the hub
    # the user put it on rather than on a row number. Switching a second hub on
    # while somebody is reading the list must not move what they had settled on.
    arrived = anyio.Event()
    keyboard = _Keyboard(Key.DOWN, arrived, Key.ENTER)
    drawn: list[tuple[int, int]] = []

    def record(devices: list[pb_client.DeviceInfo], selected: int) -> RenderableType:
        drawn.append((len(devices), selected))
        return ""

    async def devices() -> AsyncIterator[pb_client.DeviceInfo]:
        for hub in HUBS[:2]:
            yield hub
        keyboard.ready.set()
        yield HUBS[2]
        arrived.set()
        await anyio.sleep_forever()

    monkeypatch.setattr(keys, "poll_key", keyboard)
    with anyio.fail_after(5):
        chosen = await picker.choose_hub(_console(), devices(), record)

    assert chosen is not None
    assert chosen.name == "Second Hub"
    # The third hub really was listed before Enter was pressed, and the
    # selection was still on the second one when it was.
    assert (3, 1) in drawn
