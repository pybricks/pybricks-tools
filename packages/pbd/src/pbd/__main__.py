# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

import sys

from pbd import __version__


def main() -> int:
    # Placeholder entry point; replaced by the real daemon in phase 1.
    sys.stdout.write(f"pbd {__version__}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
