from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config.toml"


@dataclass(frozen=True)
class RunConfig:
    only_vms: frozenset[str]
    only_pc: str
    dry_run: bool
    keep_failed_vm_running: bool
    recreate_existing_images: bool
    redeploy_even_if_current: bool
    extra_diagnostics: bool
    rollout_without_verification: bool

    def non_default_items(self) -> list[tuple[str, object]]:
        defaults = {
            "only_vms": frozenset(),
            "only_pc": "",
            "dry_run": False,
            "keep_failed_vm_running": False,
            "recreate_existing_images": False,
            "redeploy_even_if_current": False,
            "extra_diagnostics": False,
            "rollout_without_verification": False,
        }
        current = {
            "only_vms": self.only_vms,
            "only_pc": self.only_pc,
            "dry_run": self.dry_run,
            "keep_failed_vm_running": self.keep_failed_vm_running,
            "recreate_existing_images": self.recreate_existing_images,
            "redeploy_even_if_current": self.redeploy_even_if_current,
            "extra_diagnostics": self.extra_diagnostics,
            "rollout_without_verification": self.rollout_without_verification,
        }
        return [(key, value) for key, value in current.items() if value != defaults[key]]


@dataclass(frozen=True)
class AppConfig:
    mode: str
    vm_images_dir: Path
    logs_dir: Path
    golden_image: Path
    golden_vars: Path
    student_home_content: Path | None
    browser_opens_offline_reference: bool
    optimize_image_size: bool
    course_public_source: str
    course_student_origin: str
    rollout_prepared_images_dir: Path
    rollout_windows_vm_directory: str
    rollout_windows_tools_dir: Path
    run: RunConfig

    @property
    def assignments_file(self) -> Path:
        if self.mode == "classroom":
            return REPO_ROOT / "rollout.csv"
        return REPO_ROOT / "rollout-lockdown.csv"

    @property
    def checksums_file(self) -> Path:
        if self.mode == "classroom":
            return REPO_ROOT / "checksums.sha256"
        return REPO_ROOT / "checksums-lockdown.sha256"

    @property
    def vm_suffix(self) -> str:
        return "" if self.mode == "classroom" else "-lockdown"

    @property
    def generated_hosts_dir(self) -> Path:
        return REPO_ROOT / "hosts"

    @property
    def final_continue_config(self) -> Path:
        return REPO_ROOT / "assets" / "continue" / "config.yaml"


def _table(data: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"config.toml: [{name}] must be a table")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"config.toml: unknown key(s) in [{name}]: {', '.join(unknown)}")
    return value


def _required_str(table: dict[str, Any], key: str, section: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"config.toml: [{section}].{key} must be a non-empty string")
    return value.strip()


def _optional_str(table: dict[str, Any], key: str, section: str, default: str = "") -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"config.toml: [{section}].{key} must be a string")
    return value.strip()


def _bool(table: dict[str, Any], key: str, section: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"config.toml: [{section}].{key} must be true or false")
    return value


def _path(value: str, *, base: Path = REPO_ROOT) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    if not path.is_file():
        raise FileNotFoundError(f"Configuration not found: {path}")

    with path.open("rb") as f:
        data = tomllib.load(f)

    allowed_sections = {"workflow", "paths", "golden_image", "images", "courses", "rollout", "run"}
    unknown_sections = sorted(set(data) - allowed_sections)
    if unknown_sections:
        raise ValueError(f"config.toml: unknown section(s): {', '.join(unknown_sections)}")

    workflow = _table(data, "workflow", {"mode"})
    paths = _table(data, "paths", {"vm_images_dir", "logs_dir"})
    golden = _table(
        data,
        "golden_image",
        {"file", "student_home_content", "browser_opens_offline_reference"},
    )
    images = _table(data, "images", {"optimize_image_size"})
    courses = _table(data, "courses", {"public_source", "student_origin"})
    rollout = _table(data, "rollout", {"prepared_images_dir", "windows_vm_directory", "windows_tools_dir"})
    run = _table(
        data,
        "run",
        {
            "only_vms",
            "only_pc",
            "dry_run",
            "keep_failed_vm_running",
            "recreate_existing_images",
            "redeploy_even_if_current",
            "extra_diagnostics",
            "rollout_without_verification",
        },
    )

    mode = _required_str(workflow, "mode", "workflow").lower()
    if mode not in {"classroom", "lockdown"}:
        raise ValueError("config.toml: [workflow].mode must be 'classroom' or 'lockdown'")

    vm_images_dir = _path(_required_str(paths, "vm_images_dir", "paths"))
    logs_dir = _path(_required_str(paths, "logs_dir", "paths"))

    golden_name = _required_str(golden, "file", "golden_image")
    golden_path = _path(golden_name, base=vm_images_dir)
    if golden_path.suffix != ".qcow2":
        raise ValueError("config.toml: [golden_image].file must name a .qcow2 image")
    golden_vars = golden_path.with_suffix(".OVMF_VARS.fd")

    home_content_raw = _optional_str(golden, "student_home_content", "golden_image")
    student_home_content = _path(home_content_raw) if home_content_raw else None

    only_raw = run.get("only_vms", [])
    if not isinstance(only_raw, list) or any(not isinstance(v, str) or not v.strip() for v in only_raw):
        raise ValueError("config.toml: [run].only_vms must be an array of non-empty strings")
    only_vms = frozenset(v.strip() for v in only_raw)

    result = AppConfig(
        mode=mode,
        vm_images_dir=vm_images_dir,
        logs_dir=logs_dir,
        golden_image=golden_path,
        golden_vars=golden_vars,
        student_home_content=student_home_content,
        browser_opens_offline_reference=_bool(
            golden, "browser_opens_offline_reference", "golden_image", True
        ),
        optimize_image_size=_bool(images, "optimize_image_size", "images", True),
        course_public_source=_required_str(courses, "public_source", "courses"),
        course_student_origin=_required_str(courses, "student_origin", "courses"),
        rollout_prepared_images_dir=_path(
            _required_str(rollout, "prepared_images_dir", "rollout")
        ),
        rollout_windows_vm_directory=_required_str(
            rollout, "windows_vm_directory", "rollout"
        ),
        rollout_windows_tools_dir=_path(
            _required_str(rollout, "windows_tools_dir", "rollout")
        ),
        run=RunConfig(
            only_vms=only_vms,
            only_pc=_optional_str(run, "only_pc", "run"),
            dry_run=_bool(run, "dry_run", "run", False),
            keep_failed_vm_running=_bool(run, "keep_failed_vm_running", "run", False),
            recreate_existing_images=_bool(run, "recreate_existing_images", "run", False),
            redeploy_even_if_current=_bool(run, "redeploy_even_if_current", "run", False),
            extra_diagnostics=_bool(run, "extra_diagnostics", "run", False),
            rollout_without_verification=_bool(run, "rollout_without_verification", "run", False),
        ),
    )

    return result


def print_run_controls(cfg: AppConfig) -> None:
    changed = cfg.run.non_default_items()
    if not changed:
        return
    print("Temporary run settings are active:")
    for key, value in changed:
        if isinstance(value, frozenset):
            rendered = "[" + ", ".join(sorted(value)) + "]"
        else:
            rendered = repr(value)
        print(f"  {key:30s} = {rendered}")
    print()
