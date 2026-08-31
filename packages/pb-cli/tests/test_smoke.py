# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

import pb_cli


def test_version_is_exposed() -> None:
    assert pb_cli.__version__
