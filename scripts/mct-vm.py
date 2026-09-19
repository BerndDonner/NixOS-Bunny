#!/usr/bin/env python3
"""MCT VM lifecycle entry point.

All tool configuration and help live in scripts/config/config.toml. The command
line intentionally accepts exactly one command and no options.
"""

from mct_vm.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
