from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from .artifacts import image_artifacts
from .config import AppConfig, REPO_ROOT
from .csv_model import read_rollout_csv, require_fields
from .golden import ensure_no_live_golden_session
from .selection import select_rows


def _selected_vms(cfg: AppConfig) -> list[str]:
    doc = read_rollout_csv(cfg.assignments_file)
    rows = select_rows(
        doc.active_rows(),
        include=cfg.run.vms_include,
        exclude=cfg.run.vms_exclude,
    )
    for row in rows:
        require_fields(row, ["vm"], command="reset")
    return [row.vm for row in rows]


def _known_vms(cfg: AppConfig) -> list[str]:
    result: set[str] = set()
    for path in (REPO_ROOT / "hosts").glob("bunny[0-9][0-9].nix"):
        result.add(path.stem)

    # Also include stale artifacts for hosts that may have disappeared from the
    # current CSV/hosts directory. reset-golden/reset-finalized-golden must not
    # leave an old downstream bunnyXX image behind merely because that student
    # is no longer active.
    pattern = re.compile(r"^(bunny[0-9]{2})(?:-lockdown)?(?:\.|$)")
    if cfg.vm_images_dir.is_dir():
        for path in cfg.vm_images_dir.iterdir():
            match = pattern.match(path.name)
            if match:
                result.add(match.group(1))
    return sorted(result)


def _remove(paths: list[Path] | tuple[Path, ...], *, dry_run: bool) -> None:
    for path in paths:
        if not path.exists():
            continue
        if dry_run:
            print(f"Would remove {path}")
        else:
            path.unlink()
            print(f"Removed {path}")


def _remove_vm_family(cfg: AppConfig, *, vms: list[str], suffix: str, include_vm: bool) -> None:
    for vm in vms:
        artifacts = image_artifacts(vm, suffix)
        paths: list[Path] = []
        if include_vm:
            paths.extend(artifacts.vm_paths(cfg.vm_images_dir))
        paths.extend(artifacts.rollout_paths(cfg.vm_images_dir))
        _remove(paths, dry_run=cfg.run.dry_run)


def _remove_all_downstream(cfg: AppConfig) -> None:
    vms = _known_vms(cfg)
    for suffix in ("", "-lockdown"):
        _remove_vm_family(cfg, vms=vms, suffix=suffix, include_vm=True)


def reset_rollout_images(cfg: AppConfig) -> int:
    vms = _selected_vms(cfg)
    if not vms:
        print(f"WARN: no VMs selected from {cfg.assignments_file}")
        return 0
    print(f"Reset rollout images for mode={cfg.mode}: {', '.join(vms)}")
    _remove_vm_family(cfg, vms=vms, suffix=cfg.vm_suffix, include_vm=False)
    print("Nothing was rebuilt. Run `./scripts/mct-vm.py build-rollout-images` when ready.")
    return 0


def reset_vms(cfg: AppConfig) -> int:
    vms = _selected_vms(cfg)
    if not vms:
        print(f"WARN: no VMs selected from {cfg.assignments_file}")
        return 0
    print(f"Reset built VMs for mode={cfg.mode}: {', '.join(vms)}")
    _remove_vm_family(cfg, vms=vms, suffix=cfg.vm_suffix, include_vm=True)
    print("Nothing was rebuilt. Run `./scripts/mct-vm.py build-vms` when ready.")
    return 0


def reset_finalized_golden(cfg: AppConfig) -> int:
    ensure_no_live_golden_session()
    if not (cfg.golden_image.is_file() and cfg.golden_vars.is_file()):
        raise RuntimeError(
            "reset-finalized-golden requires the protected manual golden image + UEFI state to exist. "
            "Refusing to delete the finalized fallback when its source is missing."
        )
    print("Reset finalized golden and all derived VM/rollout artifacts.")
    _remove(
        [
            cfg.golden_finalizing_image,
            cfg.golden_finalizing_vars,
            cfg.golden_finalized_image,
            cfg.golden_finalized_vars,
        ],
        dry_run=cfg.run.dry_run,
    )
    _remove_all_downstream(cfg)
    print("Protected manual golden was kept.")
    print("Nothing was rebuilt. Run `./scripts/mct-vm.py finalize-golden` when ready.")
    return 0


def reset_golden(cfg: AppConfig) -> int:
    ensure_no_live_golden_session()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = cfg.vm_images_dir / "backups" / "golden" / stamp

    valuable = [
        cfg.golden_image,
        cfg.golden_vars,
        cfg.golden_building_image,
        cfg.golden_building_vars,
    ]
    existing_valuable = [path for path in valuable if path.exists()]
    if not existing_valuable:
        # If the protected manual source was lost, keep the finalized image as a
        # last-resort recovery point instead of deleting the only usable golden.
        existing_valuable = [
            path for path in (cfg.golden_finalized_image, cfg.golden_finalized_vars) if path.exists()
        ]

    print("Reset complete golden workflow.")
    if existing_valuable:
        print(f"Manual/in-progress golden artifacts will be backed up to: {backup_dir}")
        for path in existing_valuable:
            print(f"  {path.name}")
    else:
        print("No manual/in-progress golden artifact exists to back up.")

    if cfg.run.dry_run:
        for path in existing_valuable:
            print(f"Would move {path} -> {backup_dir / path.name}")
    else:
        if existing_valuable:
            backup_dir.mkdir(parents=True, exist_ok=False)
            for path in existing_valuable:
                shutil.move(str(path), str(backup_dir / path.name))
                print(f"Backed up {path.name}")

    _remove(
        [
            cfg.golden_finalizing_image,
            cfg.golden_finalizing_vars,
            cfg.golden_finalized_image,
            cfg.golden_finalized_vars,
        ],
        dry_run=cfg.run.dry_run,
    )
    _remove_all_downstream(cfg)
    print("Nothing was rebuilt. Run `./scripts/mct-vm.py build-golden` when ready.")
    return 0
