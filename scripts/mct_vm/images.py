from __future__ import annotations

import shutil
import subprocess

from .artifacts import image_artifacts, verify_checksum_sidecar, write_checksum_sidecar
from .config import AppConfig
from .csv_model import CsvRow, read_rollout_csv, require_fields
from .selection import select_rows


def warn(message: str) -> None:
    print(f"WARN:  {message}")


def _need_cmd(name: str) -> None:
    if shutil.which(name) is None:
        raise FileNotFoundError(f"Missing required command in PATH: {name}")


def _selected_rows(cfg: AppConfig) -> list[CsvRow]:
    doc = read_rollout_csv(cfg.assignments_file)
    return select_rows(
        doc.active_rows(),
        include=cfg.run.vms_include,
        exclude=cfg.run.vms_exclude,
    )


def build_rollout_images(cfg: AppConfig) -> int:
    rows = _selected_rows(cfg)
    if not rows:
        warn(f"No VMs selected from {cfg.assignments_file}")
        return 0

    for row in rows:
        require_fields(row, ["vm"], command="build-rollout-images")
        artifacts = image_artifacts(row.vm, cfg.vm_suffix)
        qcow2 = artifacts.qcow2(cfg.vm_artifacts_dir)
        vars_file = artifacts.vars(cfg.vm_artifacts_dir)
        vmdk_tmp = artifacts.building_vmdk(cfg.vm_artifacts_dir)
        zst_tmp = artifacts.building_compressed(cfg.vm_artifacts_dir)
        zst = artifacts.compressed(cfg.vm_artifacts_dir)
        sidecar = artifacts.checksum(cfg.vm_artifacts_dir)

        if qcow2.is_file() != vars_file.is_file():
            raise RuntimeError(
                f"Inconsistent finished VM artifacts for {row.vm}: expected QCOW2 and UEFI state together. "
                "Run reset-vms to rebuild this VM cleanly."
            )
        if not qcow2.is_file():
            raise FileNotFoundError(
                f"Missing finished VM for {row.vm}: {qcow2}. Run build-vms first."
            )

        if zst.is_file() and sidecar.is_file():
            sha = verify_checksum_sidecar(zst, sidecar)
            print(f"[{row.vm}] rollout image already complete; skip (sha256={sha})")
            continue

        # The checksum sidecar is the commit record for a rollout artifact.
        # A lone ZST is therefore an interrupted build, not a finished output.
        if zst.exists() or sidecar.exists():
            print(f"[{row.vm}] removing incomplete rollout output")
            if not cfg.run.dry_run:
                zst.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)

        print(f"[{row.vm}] {qcow2.name} -> {zst.name} + {sidecar.name}")
        if cfg.run.dry_run:
            continue

        vmdk_tmp.unlink(missing_ok=True)
        zst_tmp.unlink(missing_ok=True)
        _need_cmd("qemu-img")
        _need_cmd("zstd")

        print(f"[{row.vm}] converting QCOW2 -> temporary VMDK")
        subprocess.run(
            [
                "qemu-img", "convert", "-p", "-f", "qcow2", "-O", "vmdk",
                "-o", "subformat=monolithicSparse", str(qcow2), str(vmdk_tmp),
            ],
            check=True,
        )

        print(f"[{row.vm}] compressing temporary VMDK")
        subprocess.run(["zstd", "-T0", str(vmdk_tmp), "-o", str(zst_tmp)], check=True)
        if not zst_tmp.is_file():
            raise FileNotFoundError(f"Compression did not create {zst_tmp}")

        # Publish the compressed image, then atomically publish its checksum
        # sidecar. A crash between these operations leaves a lone ZST, which is
        # explicitly treated as incomplete on the next run.
        zst_tmp.replace(zst)
        sha = write_checksum_sidecar(zst)
        verify_checksum_sidecar(zst, sidecar)
        vmdk_tmp.unlink(missing_ok=True)
        print(f"[{row.vm}] rollout image complete (sha256={sha})")

    return 0
