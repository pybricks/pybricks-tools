# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    # Pinned to asyncio, as everywhere else: bleak and the Windows named pipe
    # backend are both asyncio-native, so the project never runs on trio.
    return "asyncio"
