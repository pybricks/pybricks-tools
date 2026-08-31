# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Writing firmware to a Move, City or Technic hub.

The conversation, where :mod:`pb_service.firmware.bootloader.lwp3` is the
words. It runs over the same :class:`~pb_service.hub.transport.HubLink` as
everything else, because a bootloader is reached by reading and writing
characteristics like anything else on a hub -- it is simply a different service
with different things to say.

Two of the steps here look like bugs and are workarounds. They are marked, and
each is exercised by the simulated bootloader, because a workaround nothing
tests is one that quietly stops working the day the quirk is fixed.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Final

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from pb_service.firmware.bootloader import lwp3
from pb_service.firmware.errors import FirmwareIncompatibleError
from pb_service.hub.transport import HubLink

logger = logging.getLogger(__name__)

__all__ = ["ERASE_TIMEOUT", "REPLY_TIMEOUT", "flash"]

REPLY_TIMEOUT: Final = 5.0
"""Seconds to wait for an answer to a command that has one."""

ERASE_TIMEOUT: Final = 5.0
"""Seconds to wait for the erase to be acknowledged.

Deliberately not longer, because the wait is often for a reply that is never
coming -- see :func:`_erase`.
"""

CHECKSUM_EVERY: Final = 10
"""How often to ask for the running checksum while writing.

Nothing to do with checking anything. Writes are unacknowledged, so this is
the only way to find out whether the hub is keeping up, and it throttles the
sender to a rate the hub can take. Borrowed from ``pybricksdev``, which
arrived at the number against real hubs.
"""

CHECKSUM_TIMEOUT: Final = 2.0
"""Seconds to wait for one of those. Short: it is a pacing device."""

Progress = Callable[[int, int], None]
"""Called with the bytes written so far and the total."""


class _Replies:
    """Collects what the bootloader says back.

    Notifications arrive outside this task tree -- the radio calls a plain
    function -- so the handler may only hand them over and must never block or
    start anything. Everything that reacts to one happens on this side.
    """

    def __init__(self) -> None:
        """Create a collector with room for a few unread replies."""
        # Bounded, because an unbounded queue here would hide the case where
        # nothing is reading. Generous enough that the pacing checksums and a
        # late erase reply can all be in flight at once.
        self._send: MemoryObjectSendStream[bytes]
        self._receive: MemoryObjectReceiveStream[bytes]
        self._send, self._receive = anyio.create_memory_object_stream[bytes](16)

    def handle(self, data: bytes) -> None:
        """Take one notification from the radio.

        Args:
            data: What arrived. Which characteristic it came from is not said,
                and does not need to be: only the bootloader's is subscribed
                to while this is running.
        """
        try:
            self._send.send_nowait(bytes(data))
        except anyio.WouldBlock:  # pragma: no cover - needs 16 unread replies
            logger.warning("dropped a bootloader reply: nobody reading")

    async def next(self) -> bytes:
        """Wait for the next reply.

        Takes no deadline of its own: cancellation belongs to a scope, so the
        caller says how long it is prepared to wait with
        [fail_after][anyio.fail_after]. That keeps the several different
        answers -- an erase that may never come, a pacing checksum, the one
        write that is acknowledged -- visible where they are decided rather
        than folded into an argument here.

        Returns:
            What arrived.
        """
        return await self._receive.receive()

    def close(self) -> None:
        """Stop collecting, and let go of the stream.

        Called when the flash is over however it ended. Without it the streams
        are only released when they are collected, which the suite is
        configured to complain about -- rightly, since a stream nobody closed
        is usually one somebody forgot to read.
        """
        self._send.close()
        self._receive.close()


async def _erase(link: HubLink, replies: _Replies, hub_type: int) -> None:
    """Erase the firmware that is there now.

    Two quirks, both from real hubs and both worked around rather than fixed
    here. The City hub's bootloader sends a write response even when the write
    asked for none, which confuses some Bluetooth stacks, so it is always
    written with a response. And on any hub the erase acknowledgement
    sometimes never arrives, or arrives much later; waiting forever would
    strand a flash that is in fact proceeding, so the wait is bounded and a
    timeout is logged and ignored.

    Args:
        link: The open connection.
        replies: Where answers arrive.
        hub_type: Which hub this is.
    """
    with_response = hub_type == 0x41
    await link.write_characteristic(
        lwp3.BOOTLOADER_CHARACTERISTIC_UUID,
        lwp3.encode_erase(),
        response=with_response,
    )
    try:
        with anyio.fail_after(ERASE_TIMEOUT):
            await replies.next()
    except TimeoutError:
        logger.info("no reply to the erase; continuing, as it usually still worked")


async def _write_image(
    link: HubLink,
    replies: _Replies,
    info: lwp3.BootloaderInfo,
    image: bytes,
    on_progress: Progress | None,
) -> tuple[int, int]:
    """Write the whole image, a chunk at a time.

    Args:
        link: The open connection.
        replies: Where answers arrive.
        info: What the bootloader said about itself.
        image: The firmware.
        on_progress: Called as each chunk lands.

    Returns:
        The checksum the hub computed and how many bytes it says it took.

    Raises:
        TimeoutError: If the hub stops keeping up, or never confirms the end.
    """
    size = info.payload_size
    address = info.start_address
    written = 0

    for index, start in enumerate(range(0, len(image), size)):
        chunk = image[start : start + size]
        last = start + size >= len(image)

        if index and index % CHECKSUM_EVERY == 0:
            # Pacing, not checking: see CHECKSUM_EVERY.
            await link.write_characteristic(
                lwp3.BOOTLOADER_CHARACTERISTIC_UUID,
                lwp3.encode_get_checksum(),
                response=False,
            )
            with anyio.fail_after(CHECKSUM_TIMEOUT):
                await replies.next()

        await link.write_characteristic(
            lwp3.BOOTLOADER_CHARACTERISTIC_UUID,
            lwp3.encode_program(address, chunk),
            response=False,
        )
        address += len(chunk)
        written += len(chunk)
        if on_progress is not None:
            on_progress(written, len(image))

        if last:
            # Only the final write is answered, which is what says the hub
            # took the whole thing.
            with anyio.fail_after(REPLY_TIMEOUT):
                return lwp3.read_program_reply(await replies.next())

    raise TimeoutError("the firmware was empty, so nothing confirmed it")


@asynccontextmanager
async def _listening(link: HubLink) -> AsyncGenerator[_Replies, None]:
    """Listen to the bootloader for the duration of the block."""
    replies = _Replies()
    await link.start_notify(lwp3.BOOTLOADER_CHARACTERISTIC_UUID, replies.handle)
    try:
        yield replies
    finally:
        replies.close()


async def flash(
    link: HubLink,
    image: bytes,
    *,
    expect_hub_type: int,
    on_progress: Progress | None = None,
) -> None:
    """Write firmware to a hub sitting in its bootloader.

    Args:
        link: An open connection to the bootloader.
        image: The firmware, already assembled and checksummed.
        expect_hub_type: What the firmware is for. Checked against what the
            bootloader says it is, and refused if they disagree.
        on_progress: Called as the image is written.

    Raises:
        FirmwareIncompatibleError: If this is not the hub the firmware is for.
        TimeoutError: If the hub stops answering.
        ValueError: If a reply is not the one that was expected.
    """
    async with _listening(link) as replies:
        await link.write_characteristic(
            lwp3.BOOTLOADER_CHARACTERISTIC_UUID, lwp3.encode_get_info(), response=True
        )
        with anyio.fail_after(REPLY_TIMEOUT):
            info = lwp3.read_info(await replies.next())
        logger.info(
            "bootloader v%d for hub type %#04x, flash %#010x-%#010x",
            info.version,
            info.hub_type,
            info.start_address,
            info.end_address,
        )

        if info.hub_type != expect_hub_type:
            # Checked against the device rather than against the advertisement
            # that led us here: this is the thing about to be written to.
            raise FirmwareIncompatibleError(
                f"this firmware is for hub type {expect_hub_type:#04x}, "
                f"and this hub is type {info.hub_type:#04x}"
            )

        await _erase(link, replies, info.hub_type)

        await link.write_characteristic(
            lwp3.BOOTLOADER_CHARACTERISTIC_UUID,
            lwp3.encode_init_loader(len(image)),
            response=True,
        )
        with anyio.fail_after(REPLY_TIMEOUT):
            await replies.next()

        checksum, count = await _write_image(link, replies, info, image, on_progress)
        logger.info("hub took %d bytes, checksum %#04x", count, checksum)
        if count != len(image):
            raise FirmwareIncompatibleError(
                f"the hub took {count} bytes of a {len(image)} byte firmware"
            )

        # No reply to this one: the hub restarts, and Bluetooth goes with it.
        await link.write_characteristic(
            lwp3.BOOTLOADER_CHARACTERISTIC_UUID,
            lwp3.encode_start_app(),
            response=False,
        )
