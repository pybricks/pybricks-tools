# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Turning an RFCOMM channel into the byte stream the link reads.

Rejoining frames that arrive in pieces is aio-rfcomm's job, in receive_until.
What is left here is the shape the link expects, and every way the channel can
end arriving as ConnectionError, which is what the link treats as a hub gone.
"""

from __future__ import annotations

from typing import cast

import aio_rfcomm
import pytest
from aio_rfcomm.errors import (
    BluetoothUnavailableError,
    ChannelClosedError,
    DelimiterNotFoundError,
)

from pb_service.hub.rfcomm.port import MAX_FRAME, ChannelPipe
from pb_service.hub.stream.pipe import BytePipe

pytestmark = pytest.mark.anyio

DELIM = b"\x02"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


class FakeChannel:
    """Answers each read with the next result, or raises it."""

    def __init__(self, *results: bytes | Exception) -> None:
        self.results = list(results)
        self.limits: list[int] = []
        self.sent: list[bytes] = []

    async def receive_until(self, delimiter: bytes, *, max_bytes: int) -> bytes:
        self.limits.append(max_bytes)
        result = self.results.pop(0)

        if isinstance(result, Exception):
            raise result

        return result

    async def send(self, data: bytes) -> None:
        result = self.results.pop(0) if self.results else None

        if isinstance(result, Exception):
            raise result

        self.sent.append(data)


def _pipe(channel: FakeChannel) -> ChannelPipe:
    return ChannelPipe(cast("aio_rfcomm.RfcommChannel", channel))


def test_it_is_the_stream_the_link_asks_for() -> None:
    # Checked by assignment rather than isinstance: BytePipe is a plain
    # Protocol, so this is pyright's job, and the test records the intent.
    pipe: BytePipe = _pipe(FakeChannel())
    assert pipe is not None


async def test_the_delimiter_is_included_because_the_link_strips_it() -> None:
    # The link does body[:-1], so handing back a frame without its delimiter
    # would silently eat the last byte of every message.
    pipe = _pipe(FakeChannel(b"abc"))
    assert await pipe.readuntil(DELIM) == b"abc" + DELIM


async def test_a_frame_is_read_with_a_bound() -> None:
    channel = FakeChannel(b"abc")
    await _pipe(channel).readuntil(DELIM)
    assert channel.limits == [MAX_FRAME]


@pytest.mark.parametrize(
    "error",
    [
        ChannelClosedError(),
        BluetoothUnavailableError(aio_rfcomm.Availability.SWITCHED_OFF),
        DelimiterNotFoundError("no delimiter"),
    ],
)
async def test_a_read_that_cannot_finish_reports_a_disconnect(error: Exception) -> None:
    pipe = _pipe(FakeChannel(error))
    with pytest.raises(ConnectionError):
        await pipe.readuntil(DELIM)


async def test_writing_reaches_the_channel() -> None:
    channel = FakeChannel()
    await _pipe(channel).write(bytearray(b"hello"))
    assert channel.sent == [b"hello"]


async def test_writing_to_a_closed_channel_reports_a_disconnect() -> None:
    pipe = _pipe(FakeChannel(ChannelClosedError()))
    with pytest.raises(ConnectionError):
        await pipe.write(b"hello")
