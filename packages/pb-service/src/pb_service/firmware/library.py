# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What the service knows about firmware, held for as long as it runs.

The catalogue and the cache belong together and both have to outlive a
request: the whole point of remembering the release listing is that flashing
three hubs in a row is one call to GitHub rather than three.

Built lazily and behind a provider, the same shape as
:mod:`pb_service.backend`. A service that never installs firmware never
imports an HTTP client, and a test can have the whole thing answer from a
canned listing by setting one environment variable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Final, final

from pb_service.firmware.cache import Cache
from pb_service.firmware.catalogue import Catalogue
from pb_service.firmware.fetch import Fetcher, Progress

logger = logging.getLogger(__name__)

__all__ = ["FETCH_ENV_VAR", "Library", "get_library"]

FETCH_ENV_VAR = "PB_FIRMWARE_FETCH"
"""Set to ``never`` to stop the service downloading firmware.

For an air-gapped machine or a build that must not reach the network. What is
already in the cache still installs; anything else becomes an error that says
where to put a file rather than a download that quietly happens.
"""


@final
class _RefusingFetcher:
    """Answers nothing, because downloading has been turned off."""

    async def fetch(self, url: str, *, on_progress: Progress | None = None) -> bytes:
        """Refuse, naming the setting responsible.

        Args:
            url: What was asked for. It goes to the log rather than into the
                message: a wrapped API URL tells a user nothing they can act
                on, and what they need to know is that a setting is doing
                this and which one.
            on_progress: Ignored; nothing arrives to report.

        Returns:
            Never.

        Raises:
            OSError: Always.
        """
        logger.info("refusing to fetch %s", url)
        raise OSError(
            f"firmware downloads are turned off by "
            f"{FETCH_ENV_VAR}={os.environ.get(FETCH_ENV_VAR)}"
        )


@dataclass(frozen=True, slots=True)
class Library:
    """The service's view of what firmware exists and what it has."""

    catalogue: Catalogue
    """What has been released, remembered between requests."""

    cache: Cache
    """What is on this computer."""


_NEVER: Final = "never"


def get_library(fetcher: Fetcher | None = None) -> Library:
    """Build the firmware library this service should use.

    Args:
        fetcher: How to reach the network, or ``None`` to decide from the
            environment. Passing one is how a test supplies a fake web.

    Returns:
        The library.
    """
    if fetcher is None:
        if os.environ.get(FETCH_ENV_VAR, "").lower() == _NEVER:
            logger.info("firmware downloads are turned off by %s", FETCH_ENV_VAR)
            fetcher = _RefusingFetcher()
        else:
            # Imported here so that a service which never touches firmware
            # never loads an HTTP client.
            from pb_service.firmware.fetch import HttpFetcher

            fetcher = HttpFetcher()

    return Library(catalogue=Catalogue(fetcher), cache=Cache(fetcher))
