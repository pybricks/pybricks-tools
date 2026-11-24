# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Names shared by both ends of the service protocol.

Notification methods and capability names, which the client and the service
have to spell the same. **Not the protocol's version**: that is a promise about
behaviour, so it belongs to the code that behaves -- pb-client declares the one
it speaks and pb-service the ones it serves. It used to be here, which let a new
pybricks-core tell an older client or service that it spoke a version it had
never implemented.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "HUB_ADAPTERS",
    "HUB_FOUND",
    "HUB_STATE",
    "HUB_STATUS",
    "HUB_STDOUT",
    "STDIO_CAPABILITY",
]

HUB_FOUND: Final = "hub.found"
"""Notification sent to a session for each advertisement seen while it scans.

Its parameters are one entry of ``hub.scan``'s result. The service sends one per
advertisement rather than one per hub, so the signal strength in it is current;
the same hub arrives many times during a scan. The name lives here because both
ends have to agree on it, and a notification whose name drifts is not an error
anywhere -- it simply stops arriving, which is the worst way to find out.
"""

HUB_ADAPTERS: Final = "hub.adapters"
"""Notification sent to a session that is scanning or connecting when the
Bluetooth adapters it cannot use change.

Its parameter ``unusable`` lists each such adapter as an ``address``, the
system's ``reason`` and, where the transport knows it, an ``availability``
word such as ``switched_off`` or ``awaiting_permission``. An empty list means
everything is usable again. Sent only on a change, and not at all while
everything is usable, so most scans never send one.

It exists for ``awaiting_permission``: macOS asking the user whether the
service may use Bluetooth. That ends by itself when the user answers, and the
search carries on, so it is said while the search is still running rather than
as an error at the end -- by which time the user would have had to notice a
dialog without being told to look for one.
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
"""Named in ``service.hello``'s reply by a service that can carry hub stdio.

Capabilities rather than a protocol version bump, because the service outlives
the install that started it: a user who upgrades and runs ``pb repl`` may still
be talking to yesterday's service. Refusing every command over a version
mismatch would be a poor trade for one added feature, so the client asks
whether this service has the feature it needs and says what to do if not.
"""
