from __future__ import annotations

import csv
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .artifacts import image_artifacts, verify_checksum_sidecar
from .config import AppConfig, REPO_ROOT, SCRIPTS_ROOT
from .csv_model import CsvRow, read_rollout_csv, require_fields
from .private_devices import PrivateDevice, validate_private_devices


@dataclass(frozen=True)
class StagedImage:
    vm: str
    image: Path
    sidecar: Path
    sha256: str


@dataclass(frozen=True)
class PrivateStagedImage:
    device: PrivateDevice
    student: CsvRow
    arch: str
    image: Path
    sidecar: Path
    sha256: str


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_destination(destination: Path) -> None:
    dest = destination.resolve()
    repo = REPO_ROOT.resolve()
    home = Path.home().resolve()

    if dest == Path(dest.anchor):
        raise ValueError(f"Refusing to stage into filesystem root: {dest}")
    if dest == home:
        raise ValueError(f"Refusing to stage directly into the home directory: {dest}")
    if _is_within(dest, repo) or _is_within(repo, dest):
        raise ValueError(
            "Rollout staging directory must be separate from the NixOS-Bunny working tree: "
            f"{dest}"
        )


def _repo_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    always = {
        ".git",
        "logs",
        ".mct-vm",
        "result",
        "__pycache__",
        "images",
        "private",
        "repos",
    }
    for name in names:
        if name in always or name.endswith(".pyc"):
            ignored.add(name)
    return ignored


def _artifact_dir(cfg: AppConfig, arch: str) -> Path:
    return cfg.vm_images_root / arch / cfg.golden_image.stem


def _preflight_school_images(cfg: AppConfig) -> list[StagedImage]:
    # The actual classroom rollout always targets Windows/amd64 school PCs,
    # independent of which architecture happened to be built most recently.
    doc = read_rollout_csv(cfg.assignments_file)
    rows = doc.active_rows()
    if not rows:
        raise ValueError(f"No active VM rows found in {cfg.assignments_file}")

    source_dir = _artifact_dir(cfg, "amd64")
    staged: list[StagedImage] = []
    for row in rows:
        require_fields(row, ["vm"], command="stage-rollout")
        artifacts = image_artifacts(row.vm, cfg.vm_suffix)
        image = artifacts.compressed(source_dir)
        sidecar = artifacts.checksum(source_dir)
        sha = verify_checksum_sidecar(image, sidecar)
        staged.append(StagedImage(row.vm, image, sidecar, sha))
    return staged


def _private_artifact(cfg: AppConfig, device: PrivateDevice) -> tuple[str, Path, Path]:
    artifacts = image_artifacts(device.vm)
    source_dir = _artifact_dir(cfg, device.arch)
    if device.artifact_format == "vmdk":
        return (
            device.arch,
            artifacts.compressed(source_dir),
            artifacts.checksum(source_dir),
        )
    if device.artifact_format == "qcow2":
        return (
            device.arch,
            artifacts.compressed_qcow2(source_dir),
            artifacts.compressed_qcow2_checksum(source_dir),
        )
    raise AssertionError(device.artifact_format)


def _preflight_private_images(cfg: AppConfig) -> list[PrivateStagedImage]:
    if cfg.mode != "classroom":
        return []

    devices = validate_private_devices(cfg)
    classroom = read_rollout_csv(cfg.host_assignments_file).active_rows()
    rows_by_vm = {row.vm: row for row in classroom}

    result: list[PrivateStagedImage] = []
    for device in devices:
        student = rows_by_vm[device.vm]
        require_fields(
            student,
            ["vm", "course", "forgejo", "full_name"],
            command="stage-rollout private",
        )
        arch, image, sidecar = _private_artifact(cfg, device)
        sha = verify_checksum_sidecar(image, sidecar)
        result.append(
            PrivateStagedImage(
                device=device,
                student=student,
                arch=arch,
                image=image,
                sidecar=sidecar,
                sha256=sha,
            )
        )
    return result


def _prepare_staged_config(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8")

    text, count = re.subn(
        r'(?m)^staging_dir\s*=.*$',
        'staging_dir = ""',
        text,
    )
    if count != 1:
        raise RuntimeError(
            f"Expected exactly one staging_dir setting in staged config, found {count}: {config_path}"
        )

    # The root of the SSD is the Windows school rollout. It must always use the
    # amd64 lineage even if stage-rollout was invoked while the build config was
    # temporarily set to arm64.
    text, count = re.subn(
        r'(?m)^arch\s*=\s*"(?:amd64|arm64)"\s*$',
        'arch = "amd64"',
        text,
    )
    if count != 1:
        raise RuntimeError(
            f"Expected exactly one workflow arch setting in staged config, found {count}: {config_path}"
        )

    config_path.write_text(text, encoding="utf-8")


def _safe_student_dir(row: CsvRow) -> str:
    forgejo = row.raw["forgejo"].strip()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", forgejo).strip("._-")
    if not slug:
        slug = "student"
    return f"{slug}-{row.vm}"


def _write_private_area(destination: Path, items: list[PrivateStagedImage]) -> None:
    private_root = destination / "private"
    if private_root.exists():
        shutil.rmtree(private_root)
    (private_root / "images" / "amd64").mkdir(parents=True, exist_ok=True)
    (private_root / "images" / "arm64").mkdir(parents=True, exist_ok=True)
    (private_root / "students").mkdir(parents=True, exist_ok=True)

    # The private area is deliberately self-contained, even if that duplicates
    # a Windows VMDK already present in the school rollout's root images/ dir.
    copied: dict[tuple[str, str], tuple[Path, Path]] = {}
    for index, item in enumerate(items, start=1):
        key = (item.arch, item.image.name)
        image_dir = private_root / "images" / item.arch
        dst_image = image_dir / item.image.name
        dst_sidecar = image_dir / item.sidecar.name

        if key not in copied:
            print(f"[private {index}/{len(items)}] Copy {item.arch}/{item.image.name}")
            shutil.copyfile(item.image, dst_image)
            shutil.copy2(item.sidecar, dst_sidecar)
            copied_sha = verify_checksum_sidecar(dst_image, dst_sidecar)
            if copied_sha != item.sha256:
                raise RuntimeError(
                    f"Internal private staging verification mismatch for {dst_image}: "
                    f"source={item.sha256} destination={copied_sha}"
                )
            copied[key] = (dst_image, dst_sidecar)

    manifest_path = private_root / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "vm",
                "full_name",
                "forgejo",
                "course",
                "profile",
                "arch",
                "image",
                "sha256",
            ]
        )
        for item in items:
            writer.writerow(
                [
                    item.device.vm,
                    item.student.raw["full_name"].strip(),
                    item.student.raw["forgejo"].strip(),
                    item.student.raw["course"].strip(),
                    item.device.profile,
                    item.arch,
                    f"images/{item.arch}/{item.image.name}",
                    item.sha256,
                ]
            )

    by_vm: dict[str, list[PrivateStagedImage]] = {}
    for item in items:
        by_vm.setdefault(item.device.vm, []).append(item)

    for vm, vm_items in sorted(by_vm.items()):
        row = vm_items[0].student
        student_dir = private_root / "students" / _safe_student_dir(row)
        student_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            f"Schüler: {row.raw['full_name'].strip()}",
            f"Forgejo: {row.raw['forgejo'].strip()}",
            f"VM: {vm}",
            f"Kurs: {row.raw['course'].strip()}",
            "",
            "Private Geräte:",
        ]
        for item in vm_items:
            rel_image = Path("..") / ".." / "images" / item.arch / item.image.name
            rel_sidecar = Path(str(rel_image) + ".sha256")
            lines.extend(
                [
                    f"- Profil: {item.device.profile}",
                    f"  Architektur: {item.arch}",
                    f"  Image: {rel_image.as_posix()}",
                    f"  SHA256-Datei: {rel_sidecar.as_posix()}",
                    f"  SHA256: {item.sha256}",
                ]
            )
        (student_dir / "INFO.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    (private_root / "README.txt").write_text(
        "\n".join(
            [
                "NixOS-Bunny private device images",
                "",
                "All images in this directory are compressed with zstd.",
                "The .sha256 files belong to the compressed .zst files.",
                "",
                "Profiles:",
                "  windows-amd64 -> VMDK.ZST",
                "  linux-amd64   -> QCOW2.ZST",
                "  macos-arm64   -> VMDK.ZST (VMware Fusion)",
                "",
                "Use students/<forgejo>-<bunnyXX>/INFO.txt to find the correct image.",
                "VM/hypervisor configuration is intentionally still done manually.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def stage_rollout(cfg: AppConfig) -> int:
    if cfg.rollout_staging_dir is None:
        raise ValueError(
            "[rollout].staging_dir is empty. Set it to a dedicated directory on the mounted rollout SSD."
        )

    destination = cfg.rollout_staging_dir
    _validate_destination(destination)

    zstd_source = SCRIPTS_ROOT / "tools" / "zstd.exe"
    if not zstd_source.is_file():
        raise FileNotFoundError(
            f"Missing Windows rollout tool: {zstd_source}. "
            "Place zstd.exe there before staging the SSD."
        )

    print("Preflight: verifying all school deployment images and sidecar checksums...")
    school_images = _preflight_school_images(cfg)
    print(f"School preflight OK: {len(school_images)} image(s)")

    private_images = _preflight_private_images(cfg)
    if cfg.mode == "classroom":
        print(f"Private preflight OK: {len(private_images)} device profile(s)")

    print(f"Rollout staging destination: {destination}")
    print(
        "[run].vms_include/vms_exclude are intentionally ignored by stage-rollout; "
        "the SSD contains the complete active school set and all private-devices.csv entries."
    )

    if cfg.run.dry_run:
        for item in school_images:
            print(
                f"Would stage school {item.image.name} + {item.sidecar.name} "
                f"(sha256={item.sha256})"
            )
        for item in private_images:
            print(
                f"Would stage private {item.device.profile}: "
                f"{item.arch}/{item.image.name} (sha256={item.sha256})"
            )
        print(f"Would copy repository tooling to {destination}")
        print(f"Would include {zstd_source} as {destination / 'scripts' / 'tools' / 'zstd.exe'}")
        return 0

    destination.mkdir(parents=True, exist_ok=True)
    stamp = destination / "ROLLOUT-STAGED.txt"
    stamp.unlink(missing_ok=True)

    # Python code must never be merged with an older staged version: a module
    # removed or renamed in the repository must disappear from the SSD too.
    staged_scripts = destination / "scripts"
    if staged_scripts.exists():
        shutil.rmtree(staged_scripts)

    # Remove the obsolete pre-refactor root-level tools directory from an
    # older staged medium, if present. Runtime tools now live under scripts/tools.
    staged_tools = destination / "tools"
    if staged_tools.exists():
        shutil.rmtree(staged_tools)

    # Copy the complete repository/tooling layout, but never runtime state or
    # source-control internals. This makes the SSD independently runnable on a
    # Windows teacher PC with only Python and the standard Windows tools.
    shutil.copytree(
        REPO_ROOT,
        destination,
        dirs_exist_ok=True,
        ignore=_repo_ignore,
        copy_function=shutil.copy2,
    )
    _prepare_staged_config(destination / "scripts" / "config" / "config.toml")

    image_dir = destination / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    # Root images are the generated Windows school deployment set. Remove stale
    # Bunny artifacts so the SSD represents exactly the active school CSV.
    for pattern in (
        "bunny*.vmdk.zst",
        "bunny*.vmdk.zst.sha256",
        "bunny*.qcow2.zst",
        "bunny*.qcow2.zst.sha256",
    ):
        for stale in image_dir.glob(pattern):
            stale.unlink()

    for index, item in enumerate(school_images, start=1):
        dst_image = image_dir / item.image.name
        dst_sidecar = image_dir / item.sidecar.name
        print(f"[school {index}/{len(school_images)}] Copy {item.image.name}")
        shutil.copyfile(item.image, dst_image)
        shutil.copy2(item.sidecar, dst_sidecar)
        copied_sha = verify_checksum_sidecar(dst_image, dst_sidecar)
        if copied_sha != item.sha256:
            raise RuntimeError(
                f"Internal staging verification mismatch for {dst_image}: "
                f"source={item.sha256} destination={copied_sha}"
            )

    if cfg.mode == "classroom":
        _write_private_area(destination, private_images)
    else:
        private_root = destination / "private"
        if private_root.exists():
            shutil.rmtree(private_root)

    stamp.write_text(
        "\n".join(
            [
                "NixOS-Bunny rollout medium",
                f"staged={datetime.now().isoformat(timespec='seconds')}",
                f"mode={cfg.mode}",
                "school_arch=amd64",
                f"school_images={len(school_images)}",
                f"private_profiles={len(private_images)}",
                "run=python scripts\\mct-vm.py rollout",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(f"Rollout SSD staged and verified: {destination}")
    print(r"Windows command: python scripts\mct-vm.py rollout")
    return 0
