# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Version of the daemon protocol itself.

Distinct from any package version. Because the daemon outlives the command that
started it, a freshly installed client will sometimes meet a daemon started by
an older install, so both sides state this in the opening handshake and the
daemon refuses versions it cannot serve.
"""

from __future__ import annotations

from typing import Final

__all__ = ["HUB_FOUND", "MIN_PROTOCOL_VERSION", "PROTOCOL_VERSION"]

PROTOCOL_VERSION: Final = 1
"""The version this build speaks."""

MIN_PROTOCOL_VERSION: Final = 1
"""The oldest client version this build still accepts."""

HUB_FOUND: Final = "hub.found"
"""Notification sent to a session for each advertisement seen while it scans.

Its parameters are one entry of ``hub.scan``'s result. The daemon sends one per
advertisement rather than one per hub, so the signal strength in it is current;
the same hub arrives many times during a scan. The name lives here because both
ends have to agree on it, and a notification whose name drifts is not an error
anywhere -- it simply stops arriving, which is the worst way to find out.
"""
