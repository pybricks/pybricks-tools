# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Errors raised by the client library."""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "PbClientError",
    "ServiceStartError",
    "ServiceTooOld",
    "ServiceUnavailable",
]


class PbClientError(Exception):
    """Base class for every error this library raises."""


class ServiceUnavailable(PbClientError):
    """No service is running and starting one was not permitted."""


class ServiceStartError(PbClientError):
    """A service was started but never became reachable."""

    def __init__(self, endpoint: str, log_file: Path) -> None:
        """Record where the service should have appeared and where to look.

        Args:
            endpoint: The endpoint that never accepted a connection.
            log_file: The service's log file.
        """
        # The log path is the single most useful thing to print here: the
        # service has no terminal of its own, so its failure is invisible
        # otherwise.
        super().__init__(
            f"could not start the background service ({endpoint}); "
            f"see the log at {log_file}"
        )
        self.endpoint = endpoint
        self.log_file = log_file


class ServiceTooOld(PbClientError):
    """The running service does not have a feature this client needs.

    The service outlives the install that started it, so upgrading the tool does
    not upgrade what is already running. Rather than refusing every command
    over a version difference, the client asks for the one feature it needs and
    raises this when it is missing.
    """

    def __init__(self, feature: str) -> None:
        """Say which feature is missing.

        Args:
            feature: The capability the service did not advertise.
        """
        super().__init__(
            f"the background service that is running cannot {feature}; "
            f"it was started by an older version"
        )
        self.feature = feature
