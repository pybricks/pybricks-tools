# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""RFCOMM transport: a hub reached over Bluetooth Classic serial."""

from __future__ import annotations

from pb_service.hub.rfcomm.transport import RfcommHubTransport, device_id_for

__all__ = ["RfcommHubTransport", "device_id_for"]
