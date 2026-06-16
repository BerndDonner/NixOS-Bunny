#!/usr/bin/env python3
"""Classroom entry point for the MCT VM helper.

Windows:
  python mct-vm.py rollout

Linux/NixOS:
  ./mct-vm.py rollout
"""

from mct_vm.cli import main_classroom


if __name__ == "__main__":
    raise SystemExit(main_classroom())
