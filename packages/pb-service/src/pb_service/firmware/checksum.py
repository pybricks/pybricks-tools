# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The two checksums a firmware image can carry.

Which one an image wants is in its metadata, and some want neither. Both are
computed over the image as it will be written and then appended to it, so the
hub can tell a complete image from an interrupted one before it runs it.

Ported from ``tools/flash/checksum.py`` in ``pybricks-micropython``, which is
what the hubs are actually checked against. The arithmetic is the interesting
part and it is not ours to invent.
"""

from __future__ import annotations

from typing import Final

__all__ = ["crc32", "sum_complement"]

_WORD: Final = 4
"""Both checksums read the image a 32-bit word at a time."""

_MASK: Final = 0xFFFFFFFF
"""Everything here is modulo 2**32, as it is on the hub."""

_ERASED: Final = 0xFFFFFFFF
"""What a word of flash reads as when nothing has been written to it."""


def sum_complement(data: bytes, size: int) -> int:
    """Return the word that makes the whole region sum to zero.

    The hub adds up every 32-bit word of the region and expects nothing left
    over, so what is stored is the two's complement of the sum. Flash that has
    not been written to still counts, which is why the region size matters
    rather than just the length of the image: the unwritten remainder is
    counted as erased words.

    Args:
        data: The image, whose length must be a multiple of four.
        size: The size of the region the hub will check, in bytes.

    Returns:
        The value to append.

    Raises:
        ValueError: If the image leaves no room for the checksum itself.
    """
    if len(data) + _WORD > size:
        raise ValueError(f"image is {len(data)} bytes, too large for {size}")

    total = sum(
        int.from_bytes(data[at : at + _WORD], "little")
        for at in range(0, len(data), _WORD)
    )
    # Everything between the end of the image and the end of the region is
    # still part of what the hub adds up, and it reads as erased flash.
    total += _ERASED * len(range(len(data), size - _WORD, _WORD))

    total &= _MASK
    # A region that already sums to zero needs no correction, and taking the
    # complement of zero would give 2**32 rather than 0.
    return (1 << 32) - total if total else 0


# The nibble-wide table for the CRC-32 the STM32 has in hardware. It is the
# ordinary CRC-32 polynomial without the reflection or the final inversion,
# which is why a library implementation cannot be used here.
_CRC_TABLE: Final = (
    0x00000000,
    0x04C11DB7,
    0x09823B6E,
    0x0D4326D9,
    0x130476DC,
    0x17C56B6B,
    0x1A864DB2,
    0x1E475005,
    0x2608EDB8,
    0x22C9F00F,
    0x2F8AD6D6,
    0x2B4BCB61,
    0x350C9B64,
    0x31CD86D3,
    0x3C8EA00A,
    0x384FBDBD,
)


def _feed(crc: int, word: int) -> int:
    """Fold one 32-bit word into the running value, a nibble at a time."""
    crc = (crc ^ word) & _MASK
    for _ in range(8):
        crc = ((crc << 4) & _MASK) ^ _CRC_TABLE[crc >> 28]
    return crc


def crc32(data: bytes, size: int) -> int:
    """Return the STM32 hardware CRC-32 of an image.

    Args:
        data: The image, whose length must be a multiple of four.
        size: The size of the region the hub will check, in bytes.

    Returns:
        The value to append.

    Raises:
        ValueError: If the image leaves no room for the checksum, or if its
            length is not a whole number of words. The hardware reads words,
            so a trailing part-word has no defined answer rather than an
            inconvenient one.
    """
    if len(data) + _WORD > size:
        raise ValueError(f"image is {len(data)} bytes, too large for {size}")
    if len(data) % _WORD:
        raise ValueError(f"image is {len(data)} bytes, not a whole number of words")

    crc = _MASK
    for at in range(0, len(data), _WORD):
        crc = _feed(crc, int.from_bytes(data[at : at + _WORD], "little"))
    return crc
