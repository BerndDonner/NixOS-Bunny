from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from .config import AppConfig
from .csv_model import CsvRow, read_rollout_csv, require_fields


def warn(message: str) -> None:
    print(f"WARN:  {message}")


def info(message: str) -> None:
    print(message)


def _need_cmd(name: str) -> None:
    if shutil.which(name) is None:
        raise FileNotFoundError(f"Missing required command in PATH: {name}")


def _copy_qcow2(src: Path, dst: Path) -> None:
    _need_cmd("cp")
    subprocess.run(["cp", "--reflink=auto", "--sparse=always", str(src), str(dst)], check=True)


def _copy_plain(src: Path, dst: Path) -> None:
    _need_cmd("cp")
    subprocess.run(["cp", "--reflink=auto", str(src), str(dst)], check=True)


def _selected_rows(cfg: AppConfig) -> list[CsvRow]:
    doc = read_rollout_csv(cfg.assignments_file)
    active = doc.active_rows()
    if not cfg.run.only_vms:
        return active
    active_by_vm = {row.vm: row for row in active}
    missing = sorted(cfg.run.only_vms - set(active_by_vm))
    if missing:
        raise ValueError(
            "[run].only_vms contains VM(s) that are not active in "
            f"{cfg.assignments_file}: {', '.join(missing)}"
        )
    return [row for row in active if row.vm in cfg.run.only_vms]


def clone_images(cfg: AppConfig) -> int:
    rows = _selected_rows(cfg)
    if not rows:
        warn(f"No active VM rows found in {cfg.assignments_file}")
        return 0

    if not cfg.golden_image.is_file():
        raise FileNotFoundError(f"Missing golden QCOW2: {cfg.golden_image}")
    if not cfg.golden_vars.is_file():
        raise FileNotFoundError(
            f"Missing golden UEFI state: {cfg.golden_vars}. "
            "Run prepare-golden/finalize-golden first or verify the file name."
        )

    print(f"Cloning from: {cfg.golden_image}")
    print(f"UEFI state : {cfg.golden_vars}")
    print(f"Targets    : {cfg.vm_images_dir}")

    planned: list[tuple[Path, Path, Path, Path]] = []
    for row in rows:
        require_fields(row, ["vm"], command="clone")
        stem = f"{row.vm}{cfg.vm_suffix}"
        dst_qcow2 = cfg.vm_images_dir / f"{stem}.qcow2"
        dst_vars = cfg.vm_images_dir / f"{stem}.OVMF_VARS.fd"
        planned.append((cfg.golden_image, dst_qcow2, cfg.golden_vars, dst_vars))

    existing = sorted(
        {str(path) for _src1, dst1, _src2, dst2 in planned for path in (dst1, dst2) if path.exists()}
    )
    if existing and not cfg.run.recreate_existing_images:
        raise FileExistsError(
            "clone refuses to reuse existing target files. Either move/delete them or set "
            "[run].recreate_existing_images = true for this run:\n  " + "\n  ".join(existing)
        )

    if cfg.run.dry_run:
        for src_qcow2, dst_qcow2, src_vars, dst_vars in planned:
            action = "REPLACE" if dst_qcow2.exists() or dst_vars.exists() else "CREATE"
            print(f"[{action}] {src_qcow2} -> {dst_qcow2}")
            print(f"[{action}] {src_vars} -> {dst_vars}")
        return 0

    cfg.vm_images_dir.mkdir(parents=True, exist_ok=True)
    for src_qcow2, dst_qcow2, src_vars, dst_vars in planned:
        if cfg.run.recreate_existing_images:
            dst_qcow2.unlink(missing_ok=True)
            dst_vars.unlink(missing_ok=True)
        info(f"Copying {src_qcow2} -> {dst_qcow2}")
        _copy_qcow2(src_qcow2, dst_qcow2)
        info(f"Copying {src_vars} -> {dst_vars}")
        _copy_plain(src_vars, dst_vars)

    return 0


def prepare_images(cfg: AppConfig) -> int:
    doc = read_rollout_csv(cfg.assignments_file)
    active = doc.active_rows()
    if not active:
        warn(f"No active VM rows found in {cfg.assignments_file}")
        return 0

    _need_cmd("qemu-img")
    _need_cmd("zstd")

    for row in active:
        require_fields(row, ["vm"], command="prepare-images")
        stem = f"{row.vm}{cfg.vm_suffix}"
        qcow2 = cfg.vm_images_dir / f"{stem}.qcow2"
        vmdk = cfg.vm_images_dir / f"{stem}.vmdk"
        zst = cfg.vm_images_dir / f"{stem}.vmdk.zst"

        if cfg.run.dry_run:
            print(f"Would convert/compress: {qcow2} -> {vmdk} -> {zst}")
            continue

        if vmdk.exists():
            warn(f"Skipping convert: {vmdk} already exists")
        elif not qcow2.is_file():
            warn(f"Skipping convert: missing source {qcow2}")
        else:
            info(f"Converting {qcow2} -> {vmdk}")
            subprocess.run(
                [
                    "qemu-img", "convert", "-p", "-f", "qcow2", "-O", "vmdk",
                    "-o", "subformat=monolithicSparse", str(qcow2), str(vmdk),
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


def update_csv(cfg: AppConfig) -> int:
    doc = read_rollout_csv(cfg.assignments_file)
    active = doc.active_rows()
    if not active:
        warn(f"No active VM rows found in {cfg.assignments_file}")
        return 0

    checksum_lines: list[str] = []
    updates: list[tuple[CsvRow, str, str]] = []

    for row in active:
        require_fields(row, ["vm"], command="update-csv")
        stem = f"{row.vm}{cfg.vm_suffix}"
        filename = f"{stem}.vmdk.zst"
        zst_path = cfg.vm_images_dir / filename
        if not zst_path.is_file():
            raise FileNotFoundError(f"Missing compressed image for active VM {row.vm}: {zst_path}")
        sha = sha256_file(zst_path)
        updates.append((row, filename, sha))
        checksum_lines.append(f"{sha}  {filename}\n")
        info(f"{filename}: {sha}")

    if cfg.run.dry_run:
        print("Dry run: rollout CSV and checksum file were not changed.")
        return 0

    for row, filename, sha in updates:
        row.raw["file"] = filename
        row.raw["sha256"] = sha
    doc.write()
    cfg.checksums_file.write_text("".join(checksum_lines), encoding="utf-8")
    info(f"Updated {doc.path}")
    info(f"Wrote {cfg.checksums_file}")
    return 0
