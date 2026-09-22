from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS_ROOT.parent
CONFIG_DIR = SCRIPTS_ROOT / "config"
CONFIG_PATH = CONFIG_DIR / "config.toml"


@dataclass(frozen=True)
class RunConfig:
    vms_include: tuple[str, ...]
    vms_exclude: tuple[str, ...]
    rollout_include: tuple[str, ...]
    rollout_exclude: tuple[str, ...]
    dry_run: bool
    keep_failed_vm_running: bool
    redeploy_even_if_current: bool
    extra_diagnostics: bool
    rollout_without_verification: bool

    def non_default_items(self) -> list[tuple[str, object]]:
        defaults = {
            "vms_include": ("*",),
            "vms_exclude": (),
            "rollout_include": ("*",),
            "rollout_exclude": (),
            "dry_run": False,
            "keep_failed_vm_running": False,
            "redeploy_even_if_current": False,
            "extra_diagnostics": False,
            "rollout_without_verification": False,
        }
        current = {
            "vms_include": self.vms_include,
            "vms_exclude": self.vms_exclude,
            "rollout_include": self.rollout_include,
            "rollout_exclude": self.rollout_exclude,
            "dry_run": self.dry_run,
            "keep_failed_vm_running": self.keep_failed_vm_running,
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
    browser_start_page: str
    preparation_host_key: Path
    optimize_image_size: bool
    course_source: str
    course_student_origin: str
    forgejo_host: str
    forgejo_exam_owner: str
    lockdown_repo: Path | None
    rollout_prepared_images_dir: Path
    rollout_staging_dir: Path | None
    rollout_windows_vm_directory: str
    run: RunConfig

    @property
    def assignments_file(self) -> Path:
        if self.mode == "classroom":
            return CONFIG_DIR / "rollout.csv"
        return CONFIG_DIR / "rollout-lockdown.csv"

    @property
    def host_assignments_file(self) -> Path:
        """Authoritative identity mapping used to generate hosts/bunnyXX.nix."""
        return CONFIG_DIR / "rollout.csv"

    @property
    def vm_suffix(self) -> str:
        return "" if self.mode == "classroom" else "-lockdown"

    @property
    def golden_building_image(self) -> Path:
        return self.golden_image.with_name(f"{self.golden_image.stem}.building.qcow2")

    @property
    def golden_finalizing_image(self) -> Path:
        return self.golden_image.with_name(f"{self.golden_image.stem}.finalizing.qcow2")

    @property
    def golden_finalized_image(self) -> Path:
        return self.golden_image.with_name(f"{self.golden_image.stem}.finalized.qcow2")

    @staticmethod
    def _vars_for(image: Path) -> Path:
        return image.with_suffix(".OVMF_VARS.fd")

    @property
    def golden_building_vars(self) -> Path:
        return self._vars_for(self.golden_building_image)

    @property
    def golden_finalizing_vars(self) -> Path:
        return self._vars_for(self.golden_finalizing_image)

    @property
    def golden_finalized_vars(self) -> Path:
        return self._vars_for(self.golden_finalized_image)

    @property
    def vm_artifacts_dir(self) -> Path:
        """Derived per-VM artifacts for the active golden lineage."""
        return self.vm_images_dir / self.golden_image.stem

    @property
    def generated_hosts_dir(self) -> Path:
        return REPO_ROOT / "hosts"

    @property
    def final_continue_config(self) -> Path:
        return REPO_ROOT / "assets" / "continue" / "config.yaml"

    @property
    def provisioning_public_key(self) -> Path:
        return REPO_ROOT / "assets" / "ssh" / "mct-vm-setup.pub"


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


def _string_list(
    table: dict[str, Any],
    key: str,
    section: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    value = table.get(key, list(default))
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(
            f"config.toml: [{section}].{key} must be an array of non-empty strings"
        )
    return tuple(item.strip() for item in value)


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

    allowed_sections = {"workflow", "paths", "golden_image", "provisioning", "images", "courses", "forgejo", "lockdown", "rollout", "run"}
    unknown_sections = sorted(set(data) - allowed_sections)
    if unknown_sections:
        raise ValueError(f"config.toml: unknown section(s): {', '.join(unknown_sections)}")

    workflow = _table(data, "workflow", {"mode"})
    paths = _table(data, "paths", {"vm_images_dir", "logs_dir"})
    golden = _table(
        data,
        "golden_image",
        {"file", "student_home_content", "browser_start_page"},
    )
    provisioning = _table(data, "provisioning", {"preparation_host_key"})
    images = _table(data, "images", {"optimize_image_size"})
    courses = _table(data, "courses", {"source", "student_origin"})
    forgejo = _table(data, "forgejo", {"host", "exam_owner"})
    lockdown = _table(data, "lockdown", {"repo"})
    rollout = _table(
        data,
        "rollout",
        {"prepared_images_dir", "staging_dir", "windows_vm_directory"},
    )
    run = _table(
        data,
        "run",
        {
            "vms_include",
            "vms_exclude",
            "rollout_include",
            "rollout_exclude",
            "dry_run",
            "keep_failed_vm_running",
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

    staging_raw = _optional_str(rollout, "staging_dir", "rollout")
    lockdown_repo_raw = _optional_str(lockdown, "repo", "lockdown")

    result = AppConfig(
        mode=mode,
        vm_images_dir=vm_images_dir,
        logs_dir=logs_dir,
        golden_image=golden_path,
        golden_vars=golden_vars,
        student_home_content=student_home_content,
        browser_start_page=_required_str(golden, "browser_start_page", "golden_image"),
        preparation_host_key=_path(
            _required_str(provisioning, "preparation_host_key", "provisioning")
        ),
        optimize_image_size=_bool(images, "optimize_image_size", "images", True),
        course_source=_required_str(courses, "source", "courses"),
        course_student_origin=_required_str(courses, "student_origin", "courses"),
        forgejo_host=_required_str(forgejo, "host", "forgejo"),
        forgejo_exam_owner=_required_str(forgejo, "exam_owner", "forgejo"),
        lockdown_repo=_path(lockdown_repo_raw) if lockdown_repo_raw else None,
        rollout_prepared_images_dir=_path(
            _required_str(rollout, "prepared_images_dir", "rollout")
        ),
        rollout_staging_dir=_path(staging_raw) if staging_raw else None,
        rollout_windows_vm_directory=_required_str(
            rollout, "windows_vm_directory", "rollout"
        ),
        run=RunConfig(
            vms_include=_string_list(run, "vms_include", "run", ("*",)),
            vms_exclude=_string_list(run, "vms_exclude", "run", ()),
            rollout_include=_string_list(run, "rollout_include", "run", ("*",)),
            rollout_exclude=_string_list(run, "rollout_exclude", "run", ()),
            dry_run=_bool(run, "dry_run", "run", False),
            keep_failed_vm_running=_bool(run, "keep_failed_vm_running", "run", False),
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
        if isinstance(value, tuple):
            rendered = "[" + ", ".join(repr(item) for item in value) + "]"
        else:
            rendered = repr(value)
        print(f"  {key:30s} = {rendered}")
    print()
