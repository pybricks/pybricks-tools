# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

import sys

from pb import __version__


def main() -> int:
    # Placeholder entry point; replaced by the real CLI in phase 2.
    sys.stdout.write(f"pb {__version__}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
