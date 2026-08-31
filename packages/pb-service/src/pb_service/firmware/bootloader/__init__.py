# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The several ways a hub takes firmware.

One module per bootloader, because they have almost nothing in common: the
Powered Up hubs speak a LEGO protocol over Bluetooth, the SPIKE hubs speak DFU
over USB, the EV3 speaks its own over HID, and the NXT speaks Atmel's over a
serial port. What they share is only the shape of the job -- erase, write,
check, restart -- and that lives in the callers rather than in a base class
neither of them would fit.
"""
