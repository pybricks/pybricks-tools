# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Version of the service protocol itself.

Distinct from any package version. Because the service outlives the command that
started it, a freshly installed client will sometimes meet a service started by
an older install, so both sides state this in the opening handshake and the
service refuses versions it cannot serve.

**What moves this number.** An added method, field or named capability does
not: capabilities are negotiated by name, so a client newer than the running
service asks for the one feature it needs rather than failing over a number. A
*changed meaning* for something that already exists does, because there is no
way to ask about one -- both sides go on truthfully claiming the same version
while meaning different things by it, and the failure is silent. That has
happened once already: ``hub.use`` with no hub in the request meant "forget
the chosen hub" and now means "report it", and a service left running from
before the change threw away the answer to the question it was asked.

**When it moves.** In the commit that changes the protocol, whether or not a
release is anywhere near. A release is not the only way this code reaches
anyone -- it can be installed from git, and then whatever is between releases
is what somebody is running -- so the number has to be right at every commit
rather than at every tag. It is cheap: nothing outside a checkout speaks an
unreleased version, so the only effect of moving it is that a stale service
says so instead of misbehaving.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "HUB_ADAPTERS",
    "HUB_FOUND",
    "HUB_STATE",
    "HUB_STATUS",
    "HUB_STDOUT",
    "MIN_PROTOCOL_VERSION",
    "PROTOCOL_VERSION",
    "STDIO_CAPABILITY",
]

PROTOCOL_VERSION: Final = 3
"""The version this build speaks.

2 since the chosen hub became something the service remembers, which changed
what ``hub.use`` and ``hub.list`` say.
"""

MIN_PROTOCOL_VERSION: Final = 3
"""The oldest client version this build still accepts.

Equal to :data:`PROTOCOL_VERSION`, and moved with it, so that a mismatch is
refused in either direction rather than only when the client is the newer of
the two. During development the service routinely outlives several changes to
the wire in a day.

This follows :data:`PROTOCOL_VERSION` through the beta releases as well, which
is a decision rather than an oversight: the wire is still expected to move, and
while the two numbers are equal a mismatch is reported with a message naming
which half is old, instead of a stale service being tolerated and then
misbehaving.

At the first **stable** release it stops following: it stays at the oldest
version the build genuinely serves, and the gap between the two numbers becomes
the compatibility promise. Raising it is then the deliberate act of dropping
support for a released client.
"""

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
