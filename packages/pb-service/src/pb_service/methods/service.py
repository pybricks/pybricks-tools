# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Session and service-level RPC methods."""

from __future__ import annotations

import logging
import platform
import secrets
import sys
from typing import Any

from pb_core import paths
from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_core.protocol import (
    FIRMWARE_CAPABILITY,
    MIN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    STDIO_CAPABILITY,
)
from pb_service import __version__
from pb_service.methods import method, require
from pb_service.session import Session

logger = logging.getLogger(__name__)

__all__ = ["service_hello", "service_info", "service_ping", "service_shutdown"]


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
        "capabilities": [STDIO_CAPABILITY, FIRMWARE_CAPABILITY],
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
