from __future__ import annotations

import csv
from dataclasses import dataclass

from .config import AppConfig
from .csv_model import CsvRow, VM_RE, read_rollout_csv
from .selection import select_rows


PROFILE_SPECS: dict[str, tuple[str, str]] = {
    "windows-amd64": ("amd64", "vmdk"),
    "linux-amd64": ("amd64", "qcow2"),
    "macos-arm64": ("arm64", "vmdk"),
}


@dataclass(frozen=True)
class PrivateDevice:
    line_no: int
    vm: str
    profile: str
    arch: str
    artifact_format: str


def validate_private_devices(cfg: AppConfig) -> list[PrivateDevice]:
    """Read and validate scripts/config/private-devices.csv.

    The file deliberately contains only two columns: vm,profile.
    Student identity/course data remains authoritative in rollout.csv.
    Multiple different profiles may refer to the same VM, but duplicate
    vm/profile pairs are rejected.
    """
    path = cfg.private_devices_file
    if not path.is_file():
        raise FileNotFoundError(f"Private device CSV not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)

    # Ignore completely empty rows, but keep original line numbers.
    nonempty = [
        (line_no, row)
        for line_no, row in enumerate(rows, start=1)
        if row and any(cell.strip() for cell in row)
    ]
    if not nonempty:
        raise ValueError(f"{path}: expected header 'vm,profile'")

    header_line, header = nonempty[0]
    normalized_header = [cell.strip().lower() for cell in header]
    if normalized_header != ["vm", "profile"]:
        raise ValueError(
            f"{path}: line {header_line}: expected exact header 'vm,profile'"
        )

    classroom_rows = read_rollout_csv(cfg.host_assignments_file).active_rows()
    known_vms = {row.vm for row in classroom_rows}

    result: list[PrivateDevice] = []
    seen_pairs: dict[tuple[str, str], int] = {}

    for line_no, row in nonempty[1:]:
        if len(row) != 2:
            raise ValueError(
                f"{path}: line {line_no}: expected exactly 2 columns (vm,profile)"
            )

        vm, profile = (cell.strip() for cell in row)
        if not VM_RE.fullmatch(vm):
            raise ValueError(
                f"{path}: line {line_no}: invalid VM {vm!r}; expected bunnyNN"
            )
        if vm not in known_vms:
            raise ValueError(
                f"{path}: line {line_no}: {vm} is not an active VM in "
                f"{cfg.host_assignments_file}"
            )

        spec = PROFILE_SPECS.get(profile)
        if spec is None:
            supported = ", ".join(PROFILE_SPECS)
            raise ValueError(
                f"{path}: line {line_no}: unsupported profile {profile!r}; "
                f"supported profiles: {supported}"
            )

        key = (vm, profile)
        if key in seen_pairs:
            raise ValueError(
                f"{path}: duplicate private device {vm},{profile} "
                f"(lines {seen_pairs[key]} and {line_no})"
            )
        seen_pairs[key] = line_no

        arch, artifact_format = spec
        result.append(
            PrivateDevice(
                line_no=line_no,
                vm=vm,
                profile=profile,
                arch=arch,
                artifact_format=artifact_format,
            )
        )

    return result


def active_build_rows(cfg: AppConfig) -> list[CsvRow]:
    """Return the VM rows that should be built for the active architecture.

    amd64 keeps the existing classroom/lockdown selection from the active
    rollout CSV. arm64 classroom builds are demand-driven: only VMs with a
    private arm64 profile are built. The normal vms_include/vms_exclude
    filters are applied afterwards in both cases.
    """
    if cfg.arch == "arm64":
        # load_config already forbids lockdown+arm64.
        wanted_vms = {
            device.vm
            for device in validate_private_devices(cfg)
            if device.arch == "arm64"
        }
        rows = [
            row
            for row in read_rollout_csv(cfg.host_assignments_file).active_rows()
            if row.vm in wanted_vms
        ]
    else:
        rows = read_rollout_csv(cfg.assignments_file).active_rows()

    return select_rows(
        rows,
        include=cfg.run.vms_include,
        exclude=cfg.run.vms_exclude,
    )
