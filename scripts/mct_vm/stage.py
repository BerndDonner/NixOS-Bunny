from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .artifacts import image_artifacts, verify_checksum_sidecar
from .config import AppConfig, REPO_ROOT, SCRIPTS_ROOT
from .csv_model import read_rollout_csv, require_fields


@dataclass(frozen=True)
class StagedImage:
    vm: str
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
    always = {".git", "logs", ".mct-vm", "result", "__pycache__", "images"}
    for name in names:
        if name in always or name.endswith(".pyc"):
            ignored.add(name)
    return ignored


def _preflight_images(cfg: AppConfig) -> list[StagedImage]:
    doc = read_rollout_csv(cfg.assignments_file)
    rows = doc.active_rows()
    if not rows:
        raise ValueError(f"No active VM rows found in {cfg.assignments_file}")

    staged: list[StagedImage] = []
    for row in rows:
        require_fields(row, ["vm"], command="stage-rollout")
        artifacts = image_artifacts(row.vm, cfg.vm_suffix)
        image = artifacts.compressed(cfg.vm_images_dir)
        sidecar = artifacts.checksum(cfg.vm_images_dir)
        sha = verify_checksum_sidecar(image, sidecar)
        staged.append(StagedImage(row.vm, image, sidecar, sha))
    return staged


def _clear_staging_only_path(config_path: Path) -> None:
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
    config_path.write_text(text, encoding="utf-8")


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

    print("Preflight: verifying all active deployment images and sidecar checksums...")
    staged_images = _preflight_images(cfg)
    print(f"Preflight OK: {len(staged_images)} image(s)")
    print(f"Rollout staging destination: {destination}")
    print("[run].only_vms is intentionally ignored by stage-rollout; the SSD contains all active VMs.")

    if cfg.run.dry_run:
        for item in staged_images:
            print(
                f"Would stage {item.image.name} + {item.sidecar.name} "
                f"(sha256={item.sha256})"
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
    _clear_staging_only_path(destination / "scripts" / "config" / "config.toml")

    image_dir = destination / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    # Images are a generated deployment set. Remove stale Bunny artifacts from
    # previous staging runs so the SSD represents exactly the active CSV.
    for stale in image_dir.glob("bunny*.vmdk.zst"):
        stale.unlink()
    for stale in image_dir.glob("bunny*.vmdk.zst.sha256"):
        stale.unlink()

    for index, item in enumerate(staged_images, start=1):
        dst_image = image_dir / item.image.name
        dst_sidecar = image_dir / item.sidecar.name
        print(f"[{index}/{len(staged_images)}] Copy {item.image.name}")
        shutil.copyfile(item.image, dst_image)
        shutil.copy2(item.sidecar, dst_sidecar)
        copied_sha = verify_checksum_sidecar(dst_image, dst_sidecar)
        if copied_sha != item.sha256:
            raise RuntimeError(
                f"Internal staging verification mismatch for {dst_image}: "
                f"source={item.sha256} destination={copied_sha}"
            )

    stamp.write_text(
        "\n".join(
            [
                "NixOS-Bunny rollout medium",
                f"staged={datetime.now().isoformat(timespec='seconds')}",
                f"mode={cfg.mode}",
                f"images={len(staged_images)}",
                "run=python scripts\\mct-vm.py rollout",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(f"Rollout SSD staged and verified: {destination}")
    print(r"Windows command: python scripts\mct-vm.py rollout")
    return 0
