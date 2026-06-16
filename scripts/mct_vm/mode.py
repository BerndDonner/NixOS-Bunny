from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModeConfig:
    """File naming defaults for one mct-vm operating mode.

    There is intentionally no command-line profile switch. The selected mode is
    determined by the tiny entry point that was started:

      - mct-vm.py          -> classroom mode
      - mct-vm-lockdown.py -> lockdown/exam mode

    This keeps exam rollout commands explicit and avoids accidentally forgetting
    a mode flag under time pressure.
    """

    name: str
    program_name: str
    csv_path: str
    checksums_path: str
    golden_qcow2: str
    golden_vars: str
    vm_suffix: str = ""

    def vm_file_stem(self, vm: str) -> str:
        """Return the image/VMware disk stem for a CSV vm name.

        rollout*.csv keeps the canonical VM identity in the vm column, e.g.
        bunny02. Lockdown mode derives the actual image/disk names from that
        identity by appending -lockdown:

          bunny02 -> bunny02-lockdown
        """
        return f"{vm}{self.vm_suffix}"


CLASSROOM_MODE = ModeConfig(
    name="classroom",
    program_name="mct-vm.py",
    csv_path="rollout.csv",
    checksums_path="checksums.sha256",
    golden_qcow2="golden.qcow2",
    golden_vars="golden.OVMF_VARS.fd",
    vm_suffix="",
)

LOCKDOWN_MODE = ModeConfig(
    name="lockdown",
    program_name="mct-vm-lockdown.py",
    csv_path="rollout-lockdown.csv",
    checksums_path="checksums-lockdown.sha256",
    golden_qcow2="golden-lockdown.qcow2",
    golden_vars="golden-lockdown.OVMF_VARS.fd",
    vm_suffix="-lockdown",
)
