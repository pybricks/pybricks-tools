# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Waiting for a hub to come back.

A cable comes out mid-session -- caught on a chair, or moved to another
machine -- and putting it back is somebody saying they would like to carry on.
So a hub lost on a transport where that holds is waited for rather than only
reported gone, and the connection is made again without a second ``pb connect``.
See :data:`~pb_service.hub.transport.RECONNECTING_TRANSPORTS` for why that is
not every transport.

Only the waiting lives here. What to do with a hub that has come back belongs
to whoever owns the connection, so this reports and does not act: it takes a
transport and the hub that went away, and answers the one question of whether
that hub can be reached again.
"""

from __future__ import annotations

import logging
from contextlib import aclosing

from pb_service.hub.transport import DeviceInfo, HubTransport, ScanFilter

logger = logging.getLogger(__name__)

__all__ = ["wait_for_return"]


async def wait_for_return(
    transport: HubTransport, device: DeviceInfo
) -> DeviceInfo | None:
    """Wait until a hub that went away can be reached again.

    Nothing here bounds the wait, deliberately: the caller's own lifetime is
    the bound, and for the service that means its idle shutdown. A hub that is
    not connected does not hold the service open, so with nobody attached the
    wait ends when the service does -- which is the right answer for a hub that
    was unplugged and taken away, without a timeout having to be invented for
    it.

    Only the transport the hub was lost on is searched. That is what stops
    waiting for a cable from starting a radio, and it is why the transport is
    named in the filter rather than the results being sifted afterwards.

    Args:
        transport: How hubs are reached.
        device: The hub that went away, as it was last seen.

    Returns:
        The hub as it was found again, or ``None`` if the search finished
        without it. A transport that watches indefinitely never returns
        ``None``: it either finds the hub or is cancelled.
    """
    logger.info("waiting for %s to come back on %s", device.id, device.transport)

    async with aclosing(
        transport.discover(ScanFilter(transport=device.transport))
    ) as stream:
        async for found in stream:
            # Matched on the id rather than the name, because the id is what
            # names a route: over USB it is the serial number in the device's
            # own descriptor, so it survives the replugging and the port being
            # renumbered, and two hubs flashed without a name do not collide
            # the way their names would.
            if found.id == device.id:
                logger.info("%s is reachable again", device.id)
                return found

    logger.info("stopped waiting for %s", device.id)
    return None
