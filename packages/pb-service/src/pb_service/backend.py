# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Choosing how the service reaches hubs.

Imported lazily so that a service running against the simulated backend, as the
tests and CI do, never loads the Bluetooth stack at all.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import anyio

from pb_service.hub.transport import HubTransport

if TYPE_CHECKING:
    from aio_rfcomm import Bluetooth

logger = logging.getLogger(__name__)

__all__ = [
    "HUB_BACKEND_ENV_VAR",
    "PERMISSION_NAME",
    "get_hub_transport",
    "open_hub_transport",
]

HUB_BACKEND_ENV_VAR = "PB_HUB_BACKEND"
"""Selects the transport.

Left alone, the service uses every transport together, which is what lets a hub
be found whichever way it happens to be reachable. Set it to ``usb``, ``ble`` or
``rfcomm`` to use only one, or to ``fake`` for a simulated hub.
"""


PERMISSION_NAME = "Pybricks RFCOMM Service"
"""What macOS calls this service when it asks about Bluetooth.

Shown in the permission dialog and under Privacy & Security, and what the
answer is remembered against, so changing it asks every user again. pb-cli
repeats it in what it tells the user, and a test holds the two together.

Not plain "Pybricks": among the other entries in Privacy & Security that
reads as the Pybricks app or Pybricks Code, and says nothing about what is
asking or why. This is the part of pb-service that reaches EV3 and NXT hubs
over Bluetooth Classic, and only that part is asked about: BLE is bleak's,
inside the service itself.
"""


def _usb() -> HubTransport:
    from pb_service.hub.usb import UsbHubTransport

    return UsbHubTransport()


def _ble() -> HubTransport:
    from pb_service.hub.ble import BleHubTransport

    return BleHubTransport()


def _rfcomm(bluetooth: Bluetooth | None) -> tuple[HubTransport, ...]:
    if bluetooth is None:
        return ()

    from pb_service.hub.rfcomm import RfcommHubTransport

    return (RfcommHubTransport(bluetooth),)


def _backend() -> str:
    return os.environ.get(HUB_BACKEND_ENV_VAR, "").lower()


def get_hub_transport(bluetooth: Bluetooth | None = None) -> HubTransport:
    """Return the transport this service should use.

    Every import here is inside the branch that needs it, so a service running
    against the simulated backend loads neither the Bluetooth stack nor the
    serial one, and a machine using only one of the real transports does not
    pay for the other.

    Returns:
        Always a :class:`~pb_service.hub.combined.CombinedTransport`, holding
        USB and Bluetooth or whichever single transport ``PB_HUB_BACKEND``
        names. Always, even for one transport, so that choosing between
        transports and honouring a request for a particular one are implemented
        in exactly one place rather than once per backend. The simulated backend
        is what lets the whole service be exercised on a CI runner with no radio
        and nothing plugged in.

    Args:
        bluetooth: Bluetooth Classic, already open, or None to leave RFCOMM
            out. :func:`open_hub_transport` opens it when it is wanted.
    """
    from pb_service.hub.combined import CombinedTransport

    backend = _backend()

    if backend == "fake":
        from pb_service.hub.fake import FakeHub, FakeHubTransport

        logger.warning("using the simulated hub backend; no real hub will be found")
        return CombinedTransport(
            FakeHubTransport(
                FakeHub(name="Pybricks Hub", address="00:11:22:33:44:55"),
                FakeHub(name="Spare Hub", address="66:77:88:99:AA:BB"),
            )
        )

    if backend == "usb":
        return CombinedTransport(_usb())

    if backend == "ble":
        return CombinedTransport(_ble())

    if backend == "rfcomm":
        return CombinedTransport(*_rfcomm(bluetooth))

    if backend:
        logger.warning(
            "unknown %s=%r; using every transport", HUB_BACKEND_ENV_VAR, backend
        )

    # In TRANSPORT_PREFERENCE order, which is what makes USB the one chosen when
    # a hub is reachable both ways. A test holds the two together.
    return CombinedTransport(_usb(), _ble(), *_rfcomm(bluetooth))


@asynccontextmanager
async def open_hub_transport() -> AsyncGenerator[HubTransport, None]:
    """Open the transport this service should use, for as long as the block lasts.

    Bluetooth Classic is held open in this scope when RFCOMM is in use, and
    nowhere else: aio-rfcomm allows it open once per process, and everything
    opened through it stops working when its block ends, so it has to outlive
    every hub. It is opened the first time RFCOMM needs it, not here, so the
    service never waits on it to start (see
    :mod:`pb_service.hub.rfcomm.lazy`). If it cannot be opened, RFCOMM
    reports Bluetooth as unusable and the other transports carry on.

    Yields:
        What :func:`get_hub_transport` returns.
    """
    backend = _backend()

    if backend in ("fake", "usb", "ble"):
        yield get_hub_transport()
        return

    import aio_rfcomm.macos

    from pb_service.hub.rfcomm.lazy import LazyBluetooth

    # macOS asks the responsible process for Bluetooth permission, and a
    # service started from a command line has no application to speak for it,
    # so without this it is refused rather than asked. The answer is
    # remembered under the name, so changing it asks the user again.
    aio_rfcomm.macos.prompt_under_name(
        name=PERMISSION_NAME, reason="to connect to LEGO hubs over Bluetooth"
    )

    async with anyio.create_task_group() as tg:
        # Cast: it offers only the calls the RFCOMM transport makes.
        bluetooth = cast("Bluetooth", LazyBluetooth(tg))

        try:
            yield get_hub_transport(bluetooth)
        finally:
            tg.cancel_scope.cancel()
