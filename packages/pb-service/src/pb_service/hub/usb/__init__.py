# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""USB transport: a hub reached as an ordinary serial port."""

from __future__ import annotations

from pb_service.hub.usb.transport import UsbHubTransport, device_id_for

__all__ = ["UsbHubTransport", "device_id_for"]
