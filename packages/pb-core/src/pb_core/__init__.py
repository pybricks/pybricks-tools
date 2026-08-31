# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Shared building blocks for the Pybricks tool suite.

This package holds everything both the service and the client need to agree on:
the JSON-RPC wire protocol, the per-platform IPC transport, logging setup and
platform paths. It deliberately depends on neither ``pb_service`` nor ``pb_client``.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
