# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Session and service-level RPC methods."""

from __future__ import annotations

import logging
import platform
import secrets
import sys
from typing import Any, Final

from pb_core import paths
from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_core.protocol import STDIO_CAPABILITY
from pb_service import __version__
from pb_service.methods import method, require
from pb_service.session import Session

logger = logging.getLogger(__name__)

__all__ = [
    "MIN_PROTOCOL_VERSION",
    "PROTOCOL_VERSION",
    "service_hello",
    "service_info",
    "service_ping",
    "service_shutdown",
]

PROTOCOL_VERSION: Final = 4
"""The newest version of the service protocol this service serves.

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

**Where it lives.** Here, and in pb-client for the version it speaks, because
it is a promise about what this code does. It used to be read from
pybricks-core, which let a new pybricks-core make an older service claim a
version it did not implement.

2 since the chosen hub became something the service remembers, which changed
what ``hub.use`` and ``hub.list`` say. 4 since ``hub.connect`` goes to the chosen
hub and refuses several with ``HUB_AMBIGUOUS``, which a client of 3 does not
know, and says whether the hub was ``already_connected``.
"""

MIN_PROTOCOL_VERSION: Final = 4
"""The oldest client version this service still accepts.

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


@method(
    "service.hello",
    "Authenticate and negotiate the protocol version. Must be the first call.",
    needs_hello=False,
    params={
        "client": "Name of the connecting program.",
        "client_version": "Version of the connecting program.",
        "protocol_version": "Protocol version the client speaks.",
        "token": "The shared secret from the service's cookie file.",
    },
    returns="Service identity, protocol version and capabilities.",
)
async def service_hello(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Complete the handshake."""
    token: str = require(params, "token", str)
    client_version: int = require(params, "protocol_version", int)

    # Constant-time, so a wrong token cannot be recovered by timing repeated
    # connection attempts.
    if not secrets.compare_digest(token, session.service.token):
        logger.warning("rejecting client: bad authentication token")
        raise JsonRpcError(ErrorCode.NOT_AUTHENTICATED, "invalid authentication token")

    if client_version < MIN_PROTOCOL_VERSION or client_version > PROTOCOL_VERSION:
        raise JsonRpcError(
            ErrorCode.UNSUPPORTED_PROTOCOL_VERSION,
            f"client speaks protocol {client_version}; this service serves "
            f"{MIN_PROTOCOL_VERSION}..{PROTOCOL_VERSION}",
            # The numbers, so that the client can say which of the two is
            # the old one. Guessing costs the user a suggestion that
            # cannot work: stopping the service does not help anyone
            # whose pb is the older half.
            {
                "client": client_version,
                "min": MIN_PROTOCOL_VERSION,
                "max": PROTOCOL_VERSION,
            },
        )

    session.authenticated = True
    session.client_name = str(params.get("client", "unknown"))
    session.client_version = str(params.get("client_version", "unknown"))
    logger.info(
        "handshake complete: %s %s (protocol %d)",
        session.client_name,
        session.client_version,
        client_version,
    )

    return {
        "service_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "min_protocol_version": MIN_PROTOCOL_VERSION,
        "pid": __import__("os").getpid(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "binary_encoding": "base64",
        "log_file": str(paths.log_file()),
        # Named features rather than a protocol version, so that a client
        # newer than the running service can ask for the one thing it
        # needs instead of every command failing over a version number.
        "capabilities": [STDIO_CAPABILITY],
    }


@method(
    "service.ping", "Check that the service is responsive.", returns="An empty object."
)
async def service_ping(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Answer immediately."""
    session.service.mark_activity()
    return {}


@method(
    "service.info",
    "Report the service's status.",
    returns="Version, uptime, endpoint, log location and connected clients.",
)
async def service_info(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Describe the running service."""
    service = session.service
    service.mark_activity()
    return {
        "version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "pid": __import__("os").getpid(),
        "uptime_s": round(service.uptime, 3),
        "endpoint": str(service.endpoint),
        "log_file": str(paths.log_file()),
        "idle_timeout_s": service.idle_timeout,
        "clients": len(service.sessions),
        "hubs": [],
    }


@method(
    "service.shutdown",
    "Ask the service to exit.",
    params={"force": "Shut down even if hubs are connected."},
    returns="An empty object, sent before the service stops.",
)
async def service_shutdown(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Request shutdown."""
    session.service.request_shutdown(f"requested by {session.client_name}")
    return {}
