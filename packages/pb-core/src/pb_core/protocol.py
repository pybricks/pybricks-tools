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

__all__ = ["MIN_PROTOCOL_VERSION", "PROTOCOL_VERSION"]

PROTOCOL_VERSION: Final = 1
"""The version this build speaks."""

MIN_PROTOCOL_VERSION: Final = 1
"""The oldest client version this build still accepts."""
