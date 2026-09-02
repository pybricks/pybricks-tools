# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Consistent Overhead Byte Stuffing, the SPIKE Prime variant.

A CDC pipe is a raw byte stream with no message boundaries, so each direction
frames its messages. This is a port of ``lib/pbio/src/cobs.c``, and it is
deliberately not classic COBS -- close enough to look familiar and different
enough that assuming the familiar version produces a hub that never answers:

* the frame delimiter is ``0x02``, not ``0x00``;
* three values are escaped rather than one, so that ``0x01`` and ``0x02`` are
  both free to delimit, and a single code word carries the block size *and*
  which value ended the block;
* the encoded body is then XORed with ``0x03``, which keeps that value off the
  wire too, so a frame body can contain none of ``0x01``, ``0x02`` or ``0x03``.

Every frame also carries a **message type ahead of its payload**, inside the
framing, which is why these functions take and return the two separately rather
than leaving callers to slice the first byte off.
"""

from __future__ import annotations

from typing import Final, NamedTuple

__all__ = ["DELIMITER", "Frame", "decode", "encode"]

DELIMITER: Final = 0x02
"""Ends a frame. Reading up to this byte is what separates one message."""

HIGH_PRIORITY_DELIMITER: Final = 0x01
"""Starts a high-priority frame. Tolerated on decode, never sent."""

XOR: Final = 0x03
"""Applied to the encoded body so that ``0x03`` cannot appear on the wire."""

MAX_ESCAPED: Final = 0x02
"""The largest escaped value, so ``<=`` this is the test for one."""

CODE_OFFSET: Final = 2
"""Added to a block size to keep a code word clear of the escaped values."""

MAX_BLOCK_SIZE: Final = 84
"""How many bytes one code word can describe."""

NO_DELIMITER: Final = 0xFF
"""Code word for a full block that no escaped value ended."""


class Frame(NamedTuple):
    """One decoded message."""

    type: int
    """The message type, which travels as the first byte inside the framing."""

    payload: bytes
    """Everything after the type."""


def encode(message_type: int, payload: bytes = b"") -> bytes:
    """Frame a message ready to write to the hub.

    Args:
        message_type: The type byte, encoded ahead of the payload.
        payload: The message body, which may be empty.

    Returns:
        The framed bytes, including the trailing delimiter.
    """
    out = bytearray()
    code_index = len(out)
    out.append(NO_DELIMITER)
    block = 1

    for byte in bytes((message_type,)) + payload:
        if byte > MAX_ESCAPED:
            out.append(byte)
            block += 1

        if byte <= MAX_ESCAPED or block > MAX_BLOCK_SIZE:
            if byte <= MAX_ESCAPED:
                # The block ended on an escaped value, so fold both that value
                # and the block's size into the one code word. A block that
                # ended only because it filled up keeps its NO_DELIMITER.
                out[code_index] = byte * MAX_BLOCK_SIZE + block + CODE_OFFSET

            code_index = len(out)
            out.append(NO_DELIMITER)
            block = 1

    out[code_index] = block + CODE_OFFSET

    return bytes(byte ^ XOR for byte in out) + bytes((DELIMITER,))


class _Code(NamedTuple):
    """What one code word says about the block that follows it."""

    escaped: int | None
    """The value that ended the block, or ``None`` if nothing did."""

    block: int
    """How many bytes are in the block, counting the code word itself."""


def _read_code(code: int) -> _Code:
    """Split a code word into the value it carries and its block size.

    Args:
        code: The code word, already un-XORed.

    Returns:
        Its meaning.
    """
    if code == NO_DELIMITER:
        return _Code(None, MAX_BLOCK_SIZE + 1)

    escaped, block = divmod(code - CODE_OFFSET, MAX_BLOCK_SIZE)

    if block == 0:
        # A full block that still ended on an escaped value has to borrow from
        # the value it carries, so undo that.
        return _Code(escaped - 1, MAX_BLOCK_SIZE)

    return _Code(escaped, block)


def decode(body: bytes) -> Frame | None:
    """Decode one frame body.

    Args:
        body: The frame with its trailing delimiter already removed, which is
            what reading up to :data:`DELIMITER` leaves.

    Returns:
        The message, or ``None`` if the frame was empty or malformed. A frame
        carrying only a type and no payload also reads as ``None``, matching the
        firmware: every message defined today has at least one payload byte, so
        one that does not is not a message.
    """
    index = 1 if body[:1] == bytes((HIGH_PRIORITY_DELIMITER,)) else 0

    if index >= len(body):
        return None

    decoded = bytearray()
    code = _read_code(body[index] ^ XOR)
    index += 1

    while index < len(body):
        byte = body[index] ^ XOR
        index += 1
        block = code.block - 1

        if block > 0:
            decoded.append(byte)
            code = _Code(code.escaped, block)
            continue

        # The block is complete, so emit whatever value ended it and read this
        # byte as the next code word.
        if code.escaped is not None:
            decoded.append(code.escaped)

        code = _read_code(byte)

    if len(decoded) < 2:
        return None

    return Frame(decoded[0], bytes(decoded[1:]))
