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

Equal to :data:`PROTOCOL_VERSION` while the project is unreleased, and moved
with it, so that a mismatch is refused in either direction rather than only
when the client is the newer of the two. There is no released version to stay
compatible with yet, and during development the service routinely outlives
several changes to the wire in a day.

At the first release this stops following: it stays at the oldest version the
build genuinely serves, and the gap between the two numbers becomes the
compatibility promise.
"""

HUB_FOUND: Final = "hub.found"
"""Notification sent to a session for each advertisement seen while it scans.

Its parameters are one entry of ``hub.scan``'s result. The service sends one per
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

FIRMWARE_PROGRESS: Final = "firmware.progress"
"""Notification carrying how far an install has got.

The parameters are a ``phase`` -- ``fetching``, ``erasing`` or ``installing``
-- and ``done`` and ``total`` in bytes. Three phases rather than one because
they are three visibly different waits, and a bar that restarts is less
confusing than one that stalls at a third for a minute.

Sent for as long as a ``firmware.fetch``, ``firmware.flash`` or
``firmware.restore`` request is running.
"""

FIRMWARE_STEP: Final = "firmware.step"
"""Notification narrating what an install is doing between the progress bars.

The parameters are a ``step`` naming what is happening and an optional
``detail``. This is what stands in for the video a browser can show: looking
for the hub, finding it running LEGO's own firmware, asking it to reboot,
waiting for its bootloader to appear. Most of the time an install spends is
not spent moving bytes, and a bar alone would leave the user watching nothing.
"""

STDIO_CAPABILITY: Final = "stdio"
"""Named in ``service.hello``'s reply by a service that can carry hub stdio.

Capabilities rather than a protocol version bump, because the service outlives
the install that started it: a user who upgrades and runs ``pb repl`` may still
be talking to yesterday's service. Refusing every command over a version
mismatch would be a poor trade for one added feature, so the client asks
whether this service has the feature it needs and says what to do if not.
"""

FIRMWARE_CAPABILITY: Final = "firmware"
"""Named in ``service.hello``'s reply by a service that can install firmware.

A capability rather than a version bump, for the reason above: the service a
user is talking to is quite often the one that was already running before they
upgraded.
"""
