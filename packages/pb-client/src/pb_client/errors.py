# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Errors raised by the client library."""

from __future__ import annotations

from pathlib import Path

__all__ = ["DaemonStartError", "DaemonUnavailable", "PbClientError"]


class PbClientError(Exception):
    """Base class for every error this library raises."""


class DaemonUnavailable(PbClientError):
    """No daemon is running and starting one was not permitted."""


class DaemonStartError(PbClientError):
    """A daemon was started but never became reachable."""

    def __init__(self, endpoint: str, log_file: Path) -> None:
        """Record where the daemon should have appeared and where to look.

        Args:
            endpoint: The endpoint that never accepted a connection.
            log_file: The daemon's log file.
        """
        # The log path is the single most useful thing to print here: the
        # daemon has no terminal of its own, so its failure is invisible
        # otherwise.
        super().__init__(
            f"could not start the background service ({endpoint}); "
            f"see the log at {log_file}"
        )
        self.endpoint = endpoint
        self.log_file = log_file
