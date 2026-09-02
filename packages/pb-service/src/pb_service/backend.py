# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Choosing how the service reaches hubs.

Imported lazily so that a service running against the simulated backend, as the
tests and CI do, never loads the Bluetooth stack at all.
"""

from __future__ import annotations

import logging
import os

from pb_service.hub.transport import HubTransport

logger = logging.getLogger(__name__)

__all__ = ["HUB_BACKEND_ENV_VAR", "get_hub_transport"]

HUB_BACKEND_ENV_VAR = "PB_HUB_BACKEND"
"""Selects the transport.

Left alone, the service uses USB and Bluetooth together, which is what lets a
hub be found whichever way it happens to be reachable. Set it to ``usb`` or
``ble`` to use only one, or to ``fake`` for a simulated hub.
"""


def _usb() -> HubTransport:
    from pb_service.hub.usb import UsbHubTransport

    return UsbHubTransport()


def _ble() -> HubTransport:
    from pb_service.hub.ble import BleHubTransport

    return BleHubTransport()


def get_hub_transport() -> HubTransport:
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
    """
    from pb_service.hub.combined import CombinedTransport

    backend = os.environ.get(HUB_BACKEND_ENV_VAR, "").lower()

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

    if backend:
        logger.warning(
            "unknown %s=%r; using every transport", HUB_BACKEND_ENV_VAR, backend
        )

    # In TRANSPORT_PREFERENCE order, which is what makes USB the one chosen when
    # a hub is reachable both ways. A test holds the two together.
    return CombinedTransport(_usb(), _ble())
