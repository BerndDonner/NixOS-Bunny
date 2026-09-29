from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .artifacts import image_artifacts, verify_checksum_sidecar, write_checksum_sidecar
from .config import AppConfig
from .csv_model import CsvRow, require_fields
from .private_devices import active_build_rows, validate_private_devices


def warn(message: str) -> None:
    print(f"WARN:  {message}")


def _need_cmd(name: str) -> None:
    if shutil.which(name) is None:
        raise FileNotFoundError(f"Missing required command in PATH: {name}")


def _selected_rows(cfg: AppConfig) -> list[CsvRow]:
    return active_build_rows(cfg)


def _prepare_output_pair(*, image: Path, sidecar: Path, label: str, dry_run: bool) -> bool:
    """Return True when a valid finished compressed artifact already exists."""
    if image.is_file() and sidecar.is_file():
        sha = verify_checksum_sidecar(image, sidecar)
        print(f"{label} already complete; skip (sha256={sha})")
        return True

    # The checksum sidecar is the commit record. A lone image or sidecar is an
    # interrupted build and must not be mistaken for a finished artifact.
    if image.exists() or sidecar.exists():
        print(f"{label} removing incomplete compressed output")
        if not dry_run:
            image.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
    return False


def _publish_zstd(*, source: Path, building: Path, final: Path, sidecar: Path, label: str) -> None:
    building.unlink(missing_ok=True)
    print(f"{label} compressing {source.name} -> {final.name}")
    subprocess.run(["zstd", "-T0", str(source), "-o", str(building)], check=True)
    if not building.is_file():
        raise FileNotFoundError(f"Compression did not create {building}")

    building.replace(final)
    sha = write_checksum_sidecar(final)
    verify_checksum_sidecar(final, sidecar)
    print(f"{label} complete (sha256={sha})")


def _build_vmdk_zst(cfg: AppConfig, row: CsvRow) -> None:
    artifacts = image_artifacts(row.vm, cfg.vm_suffix)
    qcow2 = artifacts.qcow2(cfg.vm_artifacts_dir)
    vmdk_tmp = artifacts.building_vmdk(cfg.vm_artifacts_dir)
    zst_tmp = artifacts.building_compressed(cfg.vm_artifacts_dir)
    zst = artifacts.compressed(cfg.vm_artifacts_dir)
    sidecar = artifacts.checksum(cfg.vm_artifacts_dir)
    label = f"[{row.vm}] VMDK.ZST"

    if _prepare_output_pair(
        image=zst,
        sidecar=sidecar,
        label=label,
        dry_run=cfg.run.dry_run,
    ):
        return

    print(f"[{row.vm}] {qcow2.name} -> {zst.name} + {sidecar.name}")
    if cfg.run.dry_run:
        return

    vmdk_tmp.unlink(missing_ok=True)
    zst_tmp.unlink(missing_ok=True)
    print(f"[{row.vm}] converting QCOW2 -> temporary VMDK")
    subprocess.run(
        [
            "qemu-img",
            "convert",
            "-p",
            "-f",
            "qcow2",
            "-O",
            "vmdk",
            "-o",
            "subformat=monolithicSparse",
            str(qcow2),
            str(vmdk_tmp),
        ],
        check=True,
    )
    _publish_zstd(
        source=vmdk_tmp,
        building=zst_tmp,
        final=zst,
        sidecar=sidecar,
        label=label,
    )
    vmdk_tmp.unlink(missing_ok=True)


def _build_qcow2_zst(cfg: AppConfig, row: CsvRow) -> None:
    artifacts = image_artifacts(row.vm, cfg.vm_suffix)
    qcow2 = artifacts.qcow2(cfg.vm_artifacts_dir)
    zst_tmp = artifacts.building_compressed_qcow2(cfg.vm_artifacts_dir)
    zst = artifacts.compressed_qcow2(cfg.vm_artifacts_dir)
    sidecar = artifacts.compressed_qcow2_checksum(cfg.vm_artifacts_dir)
    label = f"[{row.vm}] QCOW2.ZST"

    if _prepare_output_pair(
        image=zst,
        sidecar=sidecar,
        label=label,
        dry_run=cfg.run.dry_run,
    ):
        return

    print(f"[{row.vm}] {qcow2.name} -> {zst.name} + {sidecar.name}")
    if cfg.run.dry_run:
        return

    _publish_zstd(
        source=qcow2,
        building=zst_tmp,
        final=zst,
        sidecar=sidecar,
        label=label,
    )


def build_rollout_images(cfg: AppConfig) -> int:
    rows = _selected_rows(cfg)
    if not rows:
        source = cfg.private_devices_file if cfg.arch == "arm64" else cfg.assignments_file
        warn(f"No VMs selected from {source}")
        return 0

    for row in rows:
        require_fields(row, ["vm"], command="build-rollout-images")
        artifacts = image_artifacts(row.vm, cfg.vm_suffix)
        qcow2 = artifacts.qcow2(cfg.vm_artifacts_dir)
        vars_file = artifacts.vars(cfg.vm_artifacts_dir)

        if qcow2.is_file() != vars_file.is_file():
            raise RuntimeError(
                f"Inconsistent finished VM artifacts for {row.vm}: expected QCOW2 and UEFI state together. "
                "Run reset-vms to rebuild this VM cleanly."
            )
        if not qcow2.is_file():
            raise FileNotFoundError(
                f"Missing finished VM for {row.vm}: {qcow2}. Run build-vms first."
            )

    if cfg.run.dry_run:
        # Still validate private-devices.csv below so dry-run catches bad input.
        pass
    else:
        _need_cmd("zstd")
        _need_cmd("qemu-img")

    # VMDK.ZST is the deployment artifact for the classroom Windows rollout
    # and for private VMware/Fusion devices. Therefore every active amd64 VM and
    # every selected arm64 VM gets one.
    for row in rows:
        _build_vmdk_zst(cfg, row)

    # Private Linux machines use QCOW2 directly. Build the additional packed
    # QCOW2 only for students whose private-device profile asks for it.
    if cfg.mode == "classroom" and cfg.arch == "amd64":
        linux_private_vms = {
            device.vm
            for device in validate_private_devices(cfg)
            if device.profile == "linux-amd64"
        }
        for row in rows:
            if row.vm in linux_private_vms:
                _build_qcow2_zst(cfg, row)

    return 0
