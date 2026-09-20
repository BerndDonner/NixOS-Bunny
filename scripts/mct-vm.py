#!/usr/bin/env python3
"""MCT VM lifecycle entry point.

All tool configuration and help live in scripts/config/config.toml. The command
line intentionally accepts exactly one command and no options.
"""

import sys


if sys.version_info < (3, 11):
    print(
        "ERROR: mct-vm requires Python 3.11 or newer (tomllib is part of the standard library).",
        file=sys.stderr,
    )
    raise SystemExit(2)

from mct_vm.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
