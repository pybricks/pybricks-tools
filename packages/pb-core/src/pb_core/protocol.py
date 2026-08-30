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

__all__ = [
    "HUB_FOUND",
    "HUB_STATE",
    "HUB_STATUS",
    "HUB_STDOUT",
    "MIN_PROTOCOL_VERSION",
    "PROTOCOL_VERSION",
    "STDIO_CAPABILITY",
]

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

HUB_STDOUT: Final = "hub.stdout"
"""Notification carrying one chunk of a hub's program output.

Sent to a session for as long as its ``hub.attach`` request is running. The
parameters are the hub's ``id``, a monotonic ``seq``, and the bytes as
base64 ``data``. Output is never decoded on the way through: it is whatever the
program wrote, and a chunk may end in the middle of a multi-byte character, so
only the client that owns a terminal is in a position to decode it.
"""

HUB_STATUS: Final = "hub.status"
"""Notification carrying a hub's status report as its flags change.

The parameters are the same status fields ``hub.info`` returns. This is what a
terminal's status line is drawn from, so it arrives as a *level* -- the whole
current status -- rather than as a description of what changed.
"""

HUB_STATE: Final = "hub.state"
"""Notification sent when a hub's connection state changes.

The parameters are the hub's ``id``, the new ``state``, and a ``reason`` where
there is one. A client attached to a hub learns from this that the hub has gone
away, which is the difference between a terminal that closes with an
explanation and one that simply stops.
"""

STDIO_CAPABILITY: Final = "stdio"
"""Named in ``daemon.hello``'s reply by a daemon that can carry hub stdio.

Capabilities rather than a protocol version bump, because the daemon outlives
the install that started it: a user who upgrades and runs ``pb repl`` may still
be talking to yesterday's daemon. Refusing every command over a version
mismatch would be a poor trade for one added feature, so the client asks
whether this daemon has the feature it needs and says what to do if not.
"""
