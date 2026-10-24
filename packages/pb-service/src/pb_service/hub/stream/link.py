# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""One hub's connection, over whatever carries the bytes.

The hub protocol above this file is written in terms of reading and writing
characteristics. A byte stream supplies none of that by itself, so this turns
those calls into framed messages and matches each reply to the request that
caused it. Nothing here knows whether the bytes came off a cable or a radio.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Final, final

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from pb_service.hub import protocol as p
from pb_service.hub.stream import cobs, wire
from pb_service.hub.stream.pipe import BytePipe
from pb_service.hub.transport import (
    HubCommandError,
    HubLink,
    NotificationHandler,
)

logger = logging.getLogger(__name__)

__all__ = ["REPLY_PATIENCE_SECONDS", "StreamLink"]

REPLY_PATIENCE_SECONDS: Final = 5.0
"""How long any one request waits before the hub is treated as unresponsive."""

_PENDING_REPLIES: Final = 8
"""Frames the reader may hold for a requester before dropping the oldest news."""


@final
class StreamLink(HubLink):
    """An open connection to one hub over a byte stream.

    Requests are serialised, one at a time. That is not caution: a hub answers
    one read at a time, and issuing several without waiting loses all but the
    last -- measured, not documented anywhere. With at most one request
    outstanding, correlating a reply needs no bookkeeping beyond checking that
    it is the one that was asked for.
    """

    def __init__(self, pipe: BytePipe, device_id: str) -> None:
        self._pipe = pipe
        self._device_id = device_id
        self._lock = anyio.Lock()
        self._handler: NotificationHandler | None = None
        self._connected = True
        self._tag = 0

        send, receive = anyio.create_memory_object_stream[cobs.Frame](_PENDING_REPLIES)
        self._replies_in: MemoryObjectSendStream[cobs.Frame] = send
        self._replies_out: MemoryObjectReceiveStream[cobs.Frame] = receive

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def read_characteristic(self, uuid: str) -> bytes:
        selector = wire.READ_SELECTORS.get(uuid)

        if selector is None:
            raise ValueError(f"no stream equivalent for characteristic {uuid}")

        async with self._lock:
            self._discard_stale()
            await self._write(wire.encode_read(selector))

            with anyio.fail_after(REPLY_PATIENCE_SECONDS):
                while True:
                    frame = await self._next_reply()

                    if frame.type != wire.InMessage.READ_REPLY:
                        continue

                    reply = wire.parse_read_reply(frame.payload)

                    if reply is not None and reply[0] == selector:
                        return reply[1]

    async def read_name(self) -> str:
        return (await self.read_characteristic(p.DEVICE_NAME_UUID)).decode(
            "utf-8", "replace"
        )

    async def write_characteristic(
        self, uuid: str, data: bytes, *, response: bool = True
    ) -> None:
        if uuid != p.PYBRICKS_COMMAND_EVENT_UUID:
            raise ValueError(f"characteristic {uuid} is not writable over a stream")

        self._tag = (self._tag + 1) % 256
        tag = self._tag

        async with self._lock:
            self._discard_stale()
            await self._write(wire.encode_command(tag, data))

            # The hub answers every command, so the answer is collected even
            # when the caller does not want one. Leaving it in the stream would
            # make it the next request's problem.
            with anyio.fail_after(REPLY_PATIENCE_SECONDS):
                status = await self._await_response(tag)

        if status and response:
            raise _rejection(status)

    async def start_notify(self, uuid: str, handler: NotificationHandler) -> None:
        if uuid != p.PYBRICKS_COMMAND_EVENT_UUID:
            raise ValueError(f"characteristic {uuid} does not notify over a stream")

        self._handler = handler

        async with self._lock:
            await self._write(wire.encode_subscribe(subscribed=True))

    async def _await_response(self, tag: int) -> int:
        """Wait for the response carrying ``tag``.

        Args:
            tag: What was sent with the command.

        Returns:
            The firmware's status code, zero for success.
        """
        while True:
            frame = await self._next_reply()

            if frame.type != wire.InMessage.RESPONSE:
                continue

            answer = wire.parse_response(frame.payload)

            if answer is not None and answer.tag == tag:
                return answer.status

    async def _next_reply(self) -> cobs.Frame:
        """Take the next frame addressed to whichever request is waiting.

        Returns:
            The frame.

        Raises:
            ConnectionError: If the link ended while waiting. The stream closing
                under a request is how a hub going away arrives here, and
                reporting it as a disconnect is what makes it the same failure a
                write reports rather than an anyio internal nobody handles.
        """
        try:
            return await self._replies_out.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError) as exc:
            self._connected = False
            raise ConnectionError(f"the hub on {self._device_id} went away") from exc

    async def _write(self, framed: bytes) -> None:
        """Send one framed message, reporting a dead link as a disconnect."""
        try:
            await self._pipe.write(framed)
        except (OSError, ConnectionError) as exc:
            self._connected = False
            raise ConnectionError(f"the hub on {self._device_id} went away") from exc

    def _discard_stale(self) -> None:
        """Drop anything left over from a request that gave up.

        A reply to a request nobody is waiting for any more would otherwise be
        read as the answer to the next one.
        """
        while True:
            try:
                self._replies_out.receive_nowait()
            except (anyio.WouldBlock, anyio.EndOfStream, anyio.ClosedResourceError):
                return

    async def run(self, on_lost: Callable[[str], None] | None) -> None:
        """Read frames for as long as the link lasts.

        Events go straight to the notification handler and everything else to
        whichever request is waiting. This is the only task reading the port, so
        nothing here needs a lock.

        Args:
            on_lost: Called with the hub's id if the link ends *by itself*, and
                deliberately not when this task is cancelled. Disconnecting on
                request unwinds through the same code, and reporting that as a
                hub going away would tell somebody to go and rescue a hub that
                is sitting there perfectly happily.
        """
        try:
            while True:
                body = await self._pipe.readuntil(bytes((cobs.DELIMITER,)))
                frame = cobs.decode(body[:-1])

                if frame is None:
                    logger.debug("%s: discarding an undecodable frame", self._device_id)
                    continue

                if frame.type == wire.InMessage.EVENT:
                    if self._handler is not None:
                        self._handler(frame.payload)

                    continue

                try:
                    self._replies_in.send_nowait(frame)
                except anyio.WouldBlock:
                    logger.debug(
                        "%s: no one is waiting for %r", self._device_id, frame.type
                    )
        except (
            OSError,
            ConnectionError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as exc:
            logger.info("%s: the link ended: %s", self._device_id, exc)

            if on_lost is not None:
                on_lost(self._device_id)
        finally:
            self._connected = False
            # Both ends: closing only the sending one leaves the receiving one
            # to be collected instead, which the suite reports as a
            # ResourceWarning, and a port opened and closed many times in a run
            # should accumulate nothing at all.
            self._replies_in.close()
            self._replies_out.close()

    @asynccontextmanager
    async def running(
        self, on_lost: Callable[[str], None] | None
    ) -> AsyncGenerator[StreamLink, None]:
        """Read the link in the background for as long as the block lasts.

        Args:
            on_lost: As for :meth:`run`.

        Yields:
            This link.

        Raises:
            ConnectionError: The hub went away while the block was using it,
                such as part way through the handshake.
        """
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self.run, on_lost)

                try:
                    yield self
                finally:
                    tg.cancel_scope.cancel()
        except ExceptionGroup as group:
            # The reader is this link's own business, so what fails while the
            # block uses the link comes out as itself, as from any other open.
            # A hub that drops mid-handshake is a ConnectionError, which the
            # caller handles, not a group it would report as an internal error.
            failures = group.exceptions
            alone = len(failures) == 1 and not isinstance(
                failures[0], BaseExceptionGroup
            )

            if alone or all(isinstance(e, ConnectionError) for e in failures):
                raise failures[0] from None

            raise


def _rejection(status: int) -> HubCommandError:
    """Turn a firmware status code into the hub's own refusal.

    Args:
        status: What the response carried.

    Returns:
        The equivalent error, naming the code where it is one we know.
    """
    try:
        error = p.HubError(status)
    except ValueError:
        return HubCommandError(status, f"hub refused the command (error {status:#04x})")

    return HubCommandError(error, f"hub refused the command: {error.name}")
