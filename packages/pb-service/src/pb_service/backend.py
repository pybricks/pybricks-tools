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
"""Selects the transport. ``ble`` by default, or ``fake`` for a simulated hub."""


def get_hub_transport() -> HubTransport:
    """Return the transport this service should use.

    Returns:
        A Bluetooth transport, or a simulated one when
        ``PB_HUB_BACKEND=fake``. The simulated backend is what lets the whole
        service be exercised on a CI runner with no radio.
    """
    backend = os.environ.get(HUB_BACKEND_ENV_VAR, "ble").lower()

    if backend == "fake":
        from pb_service.hub.fake import FakeHub, FakeHubTransport

        logger.warning("using the simulated hub backend; no real hub will be found")
        return FakeHubTransport(
            FakeHub(name="Pybricks Hub", address="00:11:22:33:44:55"),
            FakeHub(name="Spare Hub", address="66:77:88:99:AA:BB"),
        )

    from pb_service.hub.ble import BleHubTransport

    return BleHubTransport()
