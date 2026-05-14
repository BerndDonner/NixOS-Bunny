from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from .csv_model import read_rollout_csv, require_fields

GOLDEN_QCOW2 = "golden.qcow2"
GOLDEN_VARS = "golden.OVMF_VARS.fd"


def warn(message: str) -> None:
    print(f"WARN:  {message}")


def info(message: str) -> None:
    print(message)


def _need_cmd(name: str) -> None:
    if shutil.which(name) is None:
        raise FileNotFoundError(f"Missing required command in PATH: {name}")


def _copy_qcow2(src: Path, dst: Path) -> None:
    _need_cmd("cp")
    subprocess.run(
        ["cp", "--reflink=auto", "--sparse=always", str(src), str(dst)],
        check=True,
    )


def _copy_plain(src: Path, dst: Path) -> None:
    _need_cmd("cp")
    subprocess.run(
        ["cp", "--reflink=auto", str(src), str(dst)],
        check=True,
    )


def clone_images(*, csv_path: str, image_dir: str, golden_qcow2: str, golden_vars: str) -> int:
    doc = read_rollout_csv(csv_path)
    active = doc.active_rows()

    if not active:
        warn("No active VM rows found in rollout.csv")
        return 0

    image_root = Path(image_dir)
    src_qcow2 = image_root / golden_qcow2
    src_vars = image_root / golden_vars

    if not src_qcow2.is_file():
        raise FileNotFoundError(f"Missing required file: {src_qcow2}")
    if not src_vars.is_file():
        raise FileNotFoundError(f"Missing required file: {src_vars}")

    for row in active:
        require_fields(row, ["vm"], command="clone")
        vm = row.vm
        dst_qcow2 = image_root / f"{vm}.qcow2"
        dst_vars = image_root / f"{vm}.OVMF_VARS.fd"

        if dst_qcow2.exists():
            warn(f"Skipping copy: {dst_qcow2} already exists")
        else:
            info(f"Copying {src_qcow2} -> {dst_qcow2}")
            _copy_qcow2(src_qcow2, dst_qcow2)

        if dst_vars.exists():
            warn(f"Skipping copy: {dst_vars} already exists")
        else:
            info(f"Copying {src_vars} -> {dst_vars}")
            _copy_plain(src_vars, dst_vars)

    return 0


def prepare_images(*, csv_path: str, image_dir: str) -> int:
    doc = read_rollout_csv(csv_path)
    active = doc.active_rows()

    if not active:
        warn("No active VM rows found in rollout.csv")
        return 0

    _need_cmd("qemu-img")
    _need_cmd("zstd")

    image_root = Path(image_dir)

    for row in active:
        require_fields(row, ["vm"], command="prepare-images")
        vm = row.vm

        qcow2 = image_root / f"{vm}.qcow2"
        vmdk = image_root / f"{vm}.vmdk"
        zst = image_root / f"{vm}.vmdk.zst"

        if vmdk.exists():
            warn(f"Skipping convert: {vmdk} already exists")
        elif not qcow2.is_file():
            warn(f"Skipping convert: missing source {qcow2}")
        else:
            info(f"Converting {qcow2} -> {vmdk}")
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
                    str(vmdk),
                ],
                check=True,
            )

        if zst.exists():
            warn(f"Skipping zstd: {zst} already exists")
        elif not vmdk.is_file():
            warn(f"Skipping zstd: missing source {vmdk}")
        else:
            info(f"Compressing {vmdk} -> {zst}")
            subprocess.run(["zstd", "-T0", str(vmdk), "-o", str(zst)], check=True)

    return 0


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def update_csv(*, csv_path: str, image_dir: str, checksums_path: str) -> int:
    doc = read_rollout_csv(csv_path)
    active = doc.active_rows()

    if not active:
        warn("No active VM rows found in rollout.csv")
        return 0

    image_root = Path(image_dir)
    checksum_lines: list[str] = []

    for row in active:
        require_fields(row, ["vm"], command="update-csv")
        vm = row.vm
        filename = f"{vm}.vmdk.zst"
        zst_path = image_root / filename

        if not zst_path.is_file():
            raise FileNotFoundError(f"Missing compressed image for active VM {vm}: {zst_path}")

        sha = sha256_file(zst_path)
        row.raw["file"] = filename
        row.raw["sha256"] = sha
        checksum_lines.append(f"{sha}  {filename}\n")
        info(f"{filename}: {sha}")

    doc.write()

    checksums = Path(checksums_path)
    checksums.write_text("".join(checksum_lines), encoding="utf-8")

    info(f"Updated {doc.path}")
    info(f"Wrote {checksums}")

    return 0
