# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Getting bytes from the web, behind a seam a test can stand in for.

Everything above this deals in a :class:`Fetcher`, so the catalogue and the
cache can be exercised with canned answers and no network at all. That matters
more than it usually would: the alternative is a test suite that fails on an
aeroplane, and one that spends the sixty requests an hour GitHub allows an
unauthenticated caller.

Nothing here retries. A failure is reported and the caller decides, because
"could not reach GitHub" is often not an error at all -- `pb firmware list`
answers from the cache and says it could not check for anything newer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, Protocol, final

import httpx

__all__ = ["FETCH_TIMEOUT", "Fetcher", "HttpFetcher", "Progress"]

Progress = Callable[[int, int], None]
"""Called with the bytes so far and the total, where the total is known.

A total of zero means the server did not say how big the thing is, which
happens often enough that a caller has to render it rather than divide by it.
"""

FETCH_TIMEOUT: Final = 60.0
"""Seconds without progress before a transfer is given up on.

Not a limit on the whole download: the EV3 restore images are twelve megabytes
and a slow connection is not a failure. This is the gap between bytes, which
is what actually distinguishes a slow server from one that has stopped
answering.
"""

_CHUNK: Final = 64 * 1024
"""How much is read at a time, and therefore how often progress is reported."""


class Fetcher(Protocol):
    """Something that can retrieve a URL."""

    async def fetch(self, url: str, *, on_progress: Progress | None = None) -> bytes:
        """Retrieve a URL in full.

        Args:
            url: What to get.
            on_progress: Called as the bytes arrive, if the caller wants to
                show a bar.

        Returns:
            The body.

        Raises:
            OSError: If it could not be retrieved. The message is shown to a
                user, so it says what was being fetched rather than only what
                the transport thought of it.
        """
        ...


@final
class HttpFetcher:
    """A :class:`Fetcher` that really does go and get it.

    Streams rather than buffering the whole response inside the client, so
    progress can be reported while a twelve-megabyte image comes in and the
    peak memory is one copy rather than two.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        """Create a fetcher.

        Args:
            client: A client to use, or ``None`` to make one per request.
                Passing one is worth it when several things are fetched in a
                row, since it keeps the connection open.
        """
        self._client = client

    async def fetch(self, url: str, *, on_progress: Progress | None = None) -> bytes:
        """Retrieve a URL in full.

        Args:
            url: What to get.
            on_progress: Called as the bytes arrive.

        Returns:
            The body.

        Raises:
            OSError: If it could not be retrieved.
        """
        try:
            if self._client is not None:
                return await self._read(self._client, url, on_progress)
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT, follow_redirects=True
            ) as client:
                return await self._read(client, url, on_progress)
        except httpx.HTTPError as exc:
            # Turned into an OSError so that nothing above this module has to
            # know which HTTP library is underneath, and so the error taxonomy
            # has one family of transport failures rather than two.
            raise OSError(f"could not fetch {url}: {exc}") from exc

    @staticmethod
    async def _read(
        client: httpx.AsyncClient, url: str, on_progress: Progress | None
    ) -> bytes:
        """Stream one response into memory, reporting progress as it arrives.

        Args:
            client: The client to use.
            url: What to get.
            on_progress: Called as the bytes arrive.

        Returns:
            The body.

        Raises:
            OSError: If the server answered with an error status.
        """
        async with client.stream(
            "GET", url, timeout=FETCH_TIMEOUT, follow_redirects=True
        ) as response:
            if response.status_code >= 400:
                # Read the body first: an unread streaming response cannot be
                # closed cleanly, and the message is more useful with it.
                await response.aread()
                raise OSError(
                    f"could not fetch {url}: "
                    f"{response.status_code} {response.reason_phrase}"
                )
            total = int(response.headers.get("content-length") or 0)
            body = bytearray()
            async for chunk in response.aiter_bytes(_CHUNK):
                body.extend(chunk)
                if on_progress is not None:
                    on_progress(len(body), total)
            return bytes(body)
