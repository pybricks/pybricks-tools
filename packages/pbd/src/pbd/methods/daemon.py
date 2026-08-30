# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Session and daemon-level RPC methods."""

from __future__ import annotations

import logging
import platform
import secrets
import sys
from typing import Any

from pb_core import paths
from pb_core.jsonrpc import ErrorCode, JsonRpcError
from pb_core.protocol import (
    MIN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    STDIO_CAPABILITY,
)
from pbd import __version__
from pbd.methods import method, require
from pbd.session import Session

logger = logging.getLogger(__name__)

__all__ = ["daemon_hello", "daemon_info", "daemon_ping", "daemon_shutdown"]


@method(
    "daemon.hello",
    "Authenticate and negotiate the protocol version. Must be the first call.",
    needs_hello=False,
    params={
        "client": "Name of the connecting program.",
        "client_version": "Version of the connecting program.",
        "protocol_version": "Protocol version the client speaks.",
        "token": "The shared secret from the daemon's cookie file.",
    },
    returns="Daemon identity, protocol version and capabilities.",
)
async def daemon_hello(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Complete the handshake."""
    token: str = require(params, "token", str)
    client_version: int = require(params, "protocol_version", int)

    # Constant-time, so a wrong token cannot be recovered by timing repeated
    # connection attempts.
    if not secrets.compare_digest(token, session.daemon.token):
        logger.warning("rejecting client: bad authentication token")
        raise JsonRpcError(ErrorCode.NOT_AUTHENTICATED, "invalid authentication token")

    if client_version < MIN_PROTOCOL_VERSION or client_version > PROTOCOL_VERSION:
        raise JsonRpcError(
            ErrorCode.UNSUPPORTED_PROTOCOL_VERSION,
            f"client speaks protocol {client_version}; this daemon serves "
            f"{MIN_PROTOCOL_VERSION}..{PROTOCOL_VERSION}",
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
        "daemon_version": __version__,
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
    "daemon.ping", "Check that the daemon is responsive.", returns="An empty object."
)
async def daemon_ping(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Answer immediately."""
    session.daemon.mark_activity()
    return {}


@method(
    "daemon.info",
    "Report the daemon's status.",
    returns="Version, uptime, endpoint, log location and connected clients.",
)
async def daemon_info(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Describe the running daemon."""
    daemon = session.daemon
    daemon.mark_activity()
    return {
        "version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "pid": __import__("os").getpid(),
        "uptime_s": round(daemon.uptime, 3),
        "endpoint": str(daemon.endpoint),
        "log_file": str(paths.log_file()),
        "idle_timeout_s": daemon.idle_timeout,
        "clients": len(daemon.sessions),
        "hubs": [],
    }


@method(
    "daemon.shutdown",
    "Ask the daemon to exit.",
    params={"force": "Shut down even if hubs are connected."},
    returns="An empty object, sent before the daemon stops.",
)
async def daemon_shutdown(session: Session, params: dict[str, Any]) -> dict[str, Any]:
    """Request shutdown."""
    session.daemon.request_shutdown(f"requested by {session.client_name}")
    return {}
