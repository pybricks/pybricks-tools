# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

r"""Newline-delimited JSON framing over a byte stream.

``json.dumps`` never emits a raw newline, so a bare ``\\n`` terminator needs no
escaping.

Reading uses anyio's :class:`~anyio.streams.buffered.BufferedByteReceiveStream`,
which carries a scan offset between reads and is therefore linear overall rather
than rescanning the buffer each time.

The line limit is modest on purpose. Programs are a few kilobytes and Pybricks
hub firmware is well under a megabyte, but official restore images run to tens
of megabytes, and a single huge message would be bad for four reasons: peak
memory of several times the payload, blocking the connection's writer for the
whole transfer so no notifications can interleave (which is exactly when a
progress bar is wanted), all-or-nothing cancellation, and a memory-exhaustion
vector on a service that may be reachable over TCP. Bulk data is therefore sent
in chunks through a separate blob API rather than inline.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

import anyio
from anyio.abc import ByteStream
from anyio.streams.buffered import BufferedByteReceiveStream

from pb_core.jsonrpc.errors import ProtocolError
from pb_core.jsonrpc.messages import Message, parse_message, to_wire
from pb_core.logging import TRACE, elide

logger = logging.getLogger(__name__)

__all__ = ["MAX_LINE_BYTES", "LineFramer"]

MAX_LINE_BYTES: Final = 1024 * 1024
"""Largest single message accepted. See the module docstring."""

_DELIMITER = b"\n"


class LineFramer:
    """Reads and writes JSON-RPC messages as newline-delimited JSON.

    One task may read and one may write concurrently. Two concurrent readers,
    or two concurrent writers, are not supported: the underlying streams do not
    define that, and the session design gives every connection exactly one
    reader task and one writer task.
    """

    def __init__(self, stream: ByteStream, *, max_line: int = MAX_LINE_BYTES) -> None:
        """Wrap a byte stream.

        Args:
            stream: The transport to read from and write to.
            max_line: Largest message to accept before treating the peer as
                broken.
        """
        self._stream = stream
        self._buffered = BufferedByteReceiveStream(stream)
        self._max_line = max_line

    async def receive(self) -> Message:
        """Read one message.

        Returns:
            The parsed message.

        Raises:
            anyio.EndOfStream: If the peer closed the connection cleanly.
            anyio.BrokenResourceError: If the connection failed.
            ProtocolError: If the line is oversized or not a valid message. The
                caller should answer with a parse error and close.
        """
        try:
            line = await self._buffered.receive_until(_DELIMITER, self._max_line)
        except anyio.DelimiterNotFound as exc:
            raise ProtocolError(
                f"message exceeded {self._max_line} bytes without a newline"
            ) from exc
        except anyio.IncompleteRead as exc:
            # Peer vanished mid-message: report it as a clean end of stream so
            # callers have one shutdown path rather than two.
            raise anyio.EndOfStream from exc

        if logger.isEnabledFor(TRACE):
            logger.log(TRACE, "rx %s", elide(line.decode("utf-8", "replace")))

        try:
            decoded: Any = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"invalid JSON: {exc}") from exc

        return parse_message(decoded)

    async def send(self, message: Message) -> None:
        """Write one message.

        Args:
            message: The message to send.

        Raises:
            anyio.BrokenResourceError: If the connection failed.
        """
        payload = json.dumps(to_wire(message), separators=(",", ":")).encode("utf-8")
        if logger.isEnabledFor(TRACE):
            logger.log(TRACE, "tx %s", elide(payload.decode("utf-8", "replace")))
        await self._stream.send(payload + _DELIMITER)

    async def aclose(self) -> None:
        """Close the underlying stream."""
        await self._buffered.aclose()
