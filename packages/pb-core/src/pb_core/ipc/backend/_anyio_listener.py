# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Adapter presenting an anyio listener through the :class:`IpcListener` API."""

from __future__ import annotations

from typing import Any, final

from anyio.abc import Listener, TaskGroup
from typing_extensions import override

from pb_core.ipc.backend.provider import IpcListener, StreamHandler, owned

__all__ = ["AnyioListener"]


@final
class AnyioListener(IpcListener):
    """Wraps :class:`anyio.abc.Listener` so all backends share one interface."""

    def __init__(self, inner: Listener[Any]) -> None:
        self._inner = inner

    @override
    async def serve(self, handler: StreamHandler, task_group: TaskGroup) -> None:
        await self._inner.serve(owned(handler), task_group)

    @override
    async def aclose(self) -> None:
        await self._inner.aclose()
