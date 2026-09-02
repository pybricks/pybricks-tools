# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Several transports behaving as one.

A hub can be reachable more than one way -- a Prime hub on a desk with a cable
in it is on USB and advertising over Bluetooth at the same time -- so the
service has to look for hubs on every transport at once and then reach each one
the way it was found.

This is a :class:`~pb_service.hub.transport.HubTransport` itself, so the manager
above it holds one transport as it always did and nothing there knows there is
more than one. That is the point of the seam.

**Discovery runs every transport concurrently**, because a scan is answering
"what is out there" and a hub found on the wrong transport is still a hub. USB
tends to answer first simply by being quicker -- enumerating ports is a
directory read where a Bluetooth scan has to wait to hear something -- but that
is a tendency and not something to rely on, so anything that needs to *prefer*
a transport says so rather than taking whatever arrived first.

**One transport failing must not take the others with it.** A machine with no
Bluetooth adapter should still find a hub on a cable, and before this existed
that machine could not scan at all.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, aclosing
from typing import final

import anyio
from anyio.streams.memory import MemoryObjectSendStream

from pb_service.hub.transport import DeviceInfo, HubLink, HubTransport, ScanFilter

logger = logging.getLogger(__name__)

__all__ = ["CombinedTransport"]


@final
class CombinedTransport:
    """Presents several transports as one."""

    def __init__(self, *transports: HubTransport) -> None:
        """Combine transports, most preferred first.

        Args:
            *transports: The transports to use. The order is the preference
                order: it is the order :meth:`discover` starts them in and the
                order anything choosing between two routes to one hub should
                take them in. It does not decide what arrives first, because
                that is up to the hubs.

        Raises:
            ValueError: If no transports were given, which would be a service
                that cannot reach anything.
        """
        if not transports:
            raise ValueError("a combined transport needs at least one transport")

        self.on_disconnect: Callable[[str], None] | None = None
        self._transports = tuple(transports)

        for child in self._transports:
            child.on_disconnect = self._lost

    @property
    def scheme(self) -> str:
        """Name the transports this covers, such as ``"usb+ble"``.

        Unlike a single transport's scheme this is not an id prefix, since ids
        belong to whichever transport found the hub. It is here because the
        protocol asks for it and because it is the honest answer.
        """
        return "+".join(child.scheme for child in self._transports)

    async def discover(
        self, scan_filter: ScanFilter
    ) -> AsyncGenerator[DeviceInfo, None]:
        """Report hubs from every transport at once.

        Args:
            scan_filter: Which hubs to report.

        Yields:
            Hubs as each transport reports them, in whatever order they arrive.
        """
        # No buffer: a hub is handed straight to the caller rather than piling
        # up behind one that has stopped reading, which for a scan would mean
        # showing stale news.
        send, receive = anyio.create_memory_object_stream[DeviceInfo](0)

        async with anyio.create_task_group() as tg, receive:
            for child in self._transports:
                tg.start_soon(self._pump, child, scan_filter, send.clone())

            # The original is closed now that every child has its own clone, so
            # the receiving end ends when the last child does rather than
            # waiting on a sender nobody is using.
            send.close()

            try:
                async for device in receive:
                    yield device
            except GeneratorExit:
                # This generator owns a task group across a yield, which is the
                # one shape an async generator cannot do naively. Every caller
                # stops early -- connecting takes the first hub that matches --
                # and closing a generator throws GeneratorExit in at the yield.
                # That is a BaseException and not a cancellation, so a task
                # group's __aexit__ collects it and re-raises it inside an
                # ExceptionGroup, which surfaced as the service dying mid-scan.
                #
                # Cancelling here and returning means the task group's __aexit__
                # sees no exception at all, and GeneratorExit goes on
                # propagating out of the generator as it should. Measured
                # against the alternatives: cancelling in a `finally` does not
                # help, because the exception still reaches __aexit__, and
                # entering and exiting the group by hand does not either.
                #
                # The order matters. Cancelling *before* the receive end closes
                # is what stops the children being woken by a broken stream
                # instead of by a cancellation.
                tg.cancel_scope.cancel()
                return

            tg.cancel_scope.cancel()

    async def _pump(
        self,
        child: HubTransport,
        scan_filter: ScanFilter,
        send: MemoryObjectSendStream[DeviceInfo],
    ) -> None:
        """Forward one transport's hubs, and let it fail on its own.

        Args:
            child: The transport to read from.
            scan_filter: Which hubs to report.
            send: Where to put them. Closed when this transport is done, so
                that the last one closing ends the whole stream.
        """
        try:
            async with send, aclosing(child.discover(scan_filter)) as stream:
                async for device in stream:
                    await send.send(device)
        except Exception:
            # Deliberately broad, and the one thing it must not swallow it
            # cannot: anyio's cancellation is a BaseException, so a cancelled
            # scan still unwinds normally. What this does catch is a transport
            # that cannot work on this machine -- no Bluetooth adapter, no
            # permission on a serial port -- which must not stop the others
            # from answering. The alternative is a machine with no radio being
            # unable to find a hub that is plugged into it.
            logger.exception("the %s transport stopped scanning", child.scheme)

    def open(self, device_id: str) -> AbstractAsyncContextManager[HubLink]:
        """Connect to a hub, using the transport that its id belongs to.

        Args:
            device_id: An id previously reported by :meth:`discover`.

        Returns:
            An async context manager yielding the open link.

        Raises:
            ConnectionError: If no transport here owns that kind of id, which
                means an id from a service built with different transports --
                one stored by a client and offered back later.
        """
        for child in self._transports:
            if device_id.startswith(f"{child.scheme}:"):
                return child.open(device_id)

        raise ConnectionError(f"no transport here can reach {device_id}")

    def _lost(self, device_id: str) -> None:
        """Pass on a hub that a transport lost."""
        if self.on_disconnect is not None:
            self.on_disconnect(device_id)
