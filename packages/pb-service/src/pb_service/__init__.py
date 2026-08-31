# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The Pybricks service.

Owns every hub connection and outlives any single CLI invocation, so a hub
stays connected between commands and several clients can talk to one hub at
once.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
