#!/usr/bin/env python3
"""MCT VM lifecycle entry point.

All configuration and help live in the repository-root config.toml. The command
line intentionally accepts exactly one command and no options.
"""

from mct_vm.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
