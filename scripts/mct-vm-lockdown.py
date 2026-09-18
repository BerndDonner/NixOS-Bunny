#!/usr/bin/env python3
"""Lockdown/exam entry point for the MCT VM helper.

This is a real file, not a symlink, so it works predictably on Windows.
It uses fixed lockdown defaults; there is intentionally no --profile switch.

Windows:
  python mct-vm-lockdown.py rollout

Linux/NixOS:
  ./mct-vm-lockdown.py rollout
"""

from mct_vm.cli import main_lockdown


if __name__ == "__main__":
    raise SystemExit(main_lockdown())
