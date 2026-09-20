from __future__ import annotations

import sys

from .config import CONFIG_PATH, AppConfig, load_config, print_run_controls
from .golden import finalize_golden, prepare_golden
from .images import clone_images, prepare_images
from .individualize import individualize_images
from .nixgen import generate_nix
from .rollout import rollout_images
from .runtime import verify_provisioning_key_pair
from .stage import stage_rollout


SSH_SETUP_COMMANDS = {"prepare-golden", "finalize-golden", "individualize"}


COMMANDS = {
    "config-check",
    "generate-nix",
    "prepare-golden",
    "finalize-golden",
    "clone",
    "individualize",
    "prepare-images",
    "stage-rollout",
    "rollout",
}


def _short_usage() -> None:
    print("Usage: ./scripts/mct-vm.py <command>")
    print("Commands: " + ", ".join(sorted(COMMANDS)))
    print(f"All settings and command documentation are in {CONFIG_PATH}.")


def _config_check(cfg: AppConfig) -> int:
    print("config.toml OK")
    print(f"  mode                    : {cfg.mode}")
    print(f"  assignments             : {cfg.assignments_file}")
    print(f"  VM images               : {cfg.vm_images_dir}")
    print(f"  golden image            : {cfg.golden_image}")
    print(f"  golden UEFI state       : {cfg.golden_vars}")
    print(f"  generated hosts         : {cfg.generated_hosts_dir}")
    print(f"  logs                    : {cfg.logs_dir}")
    print(f"  student home content    : {cfg.student_home_content or '(none)'}")
    print(f"  Continue final config   : {cfg.final_continue_config}")
    print(f"  preparation host key    : {cfg.preparation_host_key}")
    print(f"  Bunny setup public key  : {cfg.provisioning_public_key}")
    print(f"  browser start page      : {cfg.browser_start_page}")
    print(f"  VM filename suffix      : {cfg.vm_suffix or '(none)'}")
    print(f"  rollout image source    : {cfg.rollout_prepared_images_dir}")
    print(f"  rollout staging dir     : {cfg.rollout_staging_dir or '(not configured)'}")
    print(f"  Windows VM directory    : {cfg.rollout_windows_vm_directory}")

    if not cfg.assignments_file.is_file():
        print(f"WARN: active assignments file does not exist: {cfg.assignments_file}")
    verify_provisioning_key_pair(
        private_key=cfg.preparation_host_key,
        public_key=cfg.provisioning_public_key,
    )
    print("  setup key pair          : OK")
    if not cfg.final_continue_config.is_file():
        print(f"WARN: final Continue config is missing: {cfg.final_continue_config}")
    if cfg.student_home_content is not None and not cfg.student_home_content.is_dir():
        print(f"WARN: student_home_content is not a directory: {cfg.student_home_content}")

    print_run_controls(cfg)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in COMMANDS:
        _short_usage()
        return 2

    try:
        cfg = load_config()
        command = args[0]

        if command in SSH_SETUP_COMMANDS:
            verify_provisioning_key_pair(
                private_key=cfg.preparation_host_key,
                public_key=cfg.provisioning_public_key,
            )

        if command != "config-check":
            print(f"Mode: {cfg.mode}")
            print_run_controls(cfg)

        if command == "config-check":
            return _config_check(cfg)
        if command == "generate-nix":
            return generate_nix(
                csv_path=str(cfg.assignments_file),
                target_dir=str(cfg.generated_hosts_dir),
                dry_run=cfg.run.dry_run,
            )
        if command == "prepare-golden":
            return prepare_golden(cfg)
        if command == "finalize-golden":
            return finalize_golden(cfg)
        if command == "clone":
            return clone_images(cfg)
        if command == "individualize":
            return individualize_images(cfg)
        if command == "prepare-images":
            return prepare_images(cfg)
        if command == "stage-rollout":
            return stage_rollout(cfg)
        if command == "rollout":
            return rollout_images(cfg)
        raise AssertionError(command)

    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
