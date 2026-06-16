from __future__ import annotations

import argparse
import sys

from . import rollout as rollout_module
from .images import clone_images, prepare_images, update_csv
from .mode import CLASSROOM_MODE, LOCKDOWN_MODE, ModeConfig
from .nixgen import generate_nix


def add_common_csv_image_options(parser: argparse.ArgumentParser, mode: ModeConfig) -> None:
    parser.add_argument(
        "--csv",
        default=mode.csv_path,
        help=f"Path to rollout CSV (default: {mode.csv_path})",
    )
    parser.add_argument(
        "--image-dir",
        default=".",
        help="Directory containing VM image files (default: current directory)",
    )


def run_integrated_rollout(argv: list[str], mode: ModeConfig) -> int:
    try:
        return int(rollout_module.main(argv, mode=mode))
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1


def build_parser(mode: ModeConfig) -> argparse.ArgumentParser:
    lockdown_note = ""
    if mode.name == "lockdown":
        lockdown_note = (
            "\nLockdown defaults:\n"
            "  CSV:        rollout-lockdown.csv\n"
            "  Checksums:  checksums-lockdown.sha256\n"
            "  Golden:     golden-lockdown.qcow2 / golden-lockdown.OVMF_VARS.fd\n"
            "  Images:     bunnyXX-lockdown.* derived from CSV vm=bunnyXX\n"
        )

    parser = argparse.ArgumentParser(
        prog=mode.program_name,
        formatter_class=argparse.RawTextHelpFormatter,
        description=(
            f"{mode.program_name} — MCT VM image and rollout helper ({mode.name} mode)\n\n"
            f"Active rows are rows in {mode.csv_path} whose first column is not commented with #.\n"
            "The vm column keeps the canonical identity, for example bunny02.\n"
            "In lockdown mode, image and deployed disk names get the -lockdown suffix.\n"
            "generate-nix is intentionally mode-neutral and still defaults to rollout.csv.\n"
            f"{lockdown_note}\n"
            "Typical workflow:\n"
            f"  {mode.program_name} clone\n"
            "  # boot each VM and run the matching nixos-rebuild inside it\n"
            f"  {mode.program_name} prepare-images\n"
            f"  {mode.program_name} update-csv\n"
            f"  {mode.program_name} generate-nix --target-dir <path>\n"
            f"  {mode.program_name} rollout --dry-run\n"
            f"  {mode.program_name} rollout\n"
        ),
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_clone = sub.add_parser(
        "clone",
        help="Clone golden qcow2 and OVMF vars for active VMs",
        description=(
            f"Clone {mode.golden_qcow2} and {mode.golden_vars} for active VMs from {mode.csv_path}.\n"
            f"Target files are named bunnyXX{mode.vm_suffix}.qcow2 and "
            f"bunnyXX{mode.vm_suffix}.OVMF_VARS.fd.\n"
            "Existing target files are skipped with a warning."
        ),
    )
    add_common_csv_image_options(p_clone, mode)
    p_clone.add_argument(
        "--golden-qcow2",
        default=mode.golden_qcow2,
        help=f"Golden qcow2 image (default: {mode.golden_qcow2})",
    )
    p_clone.add_argument(
        "--golden-vars",
        default=mode.golden_vars,
        help=f"Golden OVMF vars file (default: {mode.golden_vars})",
    )
    p_clone.set_defaults(
        func=lambda a: clone_images(
            csv_path=a.csv,
            image_dir=a.image_dir,
            golden_qcow2=a.golden_qcow2,
            golden_vars=a.golden_vars,
            vm_suffix=mode.vm_suffix,
        )
    )

    p_prepare = sub.add_parser(
        "prepare-images",
        help="Convert active qcow2 images to vmdk and compress them with zstd",
        description=(
            "Run the last image preparation step before rollout.\n"
            f"For every active VM from {mode.csv_path}:\n"
            f"  1) bunnyXX{mode.vm_suffix}.qcow2 -> bunnyXX{mode.vm_suffix}.vmdk\n"
            f"  2) bunnyXX{mode.vm_suffix}.vmdk  -> bunnyXX{mode.vm_suffix}.vmdk.zst\n\n"
            "Existing files are skipped with a warning."
        ),
    )
    add_common_csv_image_options(p_prepare, mode)
    p_prepare.set_defaults(
        func=lambda a: prepare_images(
            csv_path=a.csv,
            image_dir=a.image_dir,
            vm_suffix=mode.vm_suffix,
        )
    )

    p_update = sub.add_parser(
        "update-csv",
        help="Update file and sha256 columns in the rollout CSV for active VMs",
        description=(
            f"Update {mode.csv_path} after image preparation.\n"
            "For every active VM:\n"
            f"  file   = bunnyXX{mode.vm_suffix}.vmdk.zst\n"
            "  sha256 = SHA256 of the compressed image\n\n"
            f"Also writes {mode.checksums_path} for the active compressed images."
        ),
    )
    add_common_csv_image_options(p_update, mode)
    p_update.add_argument(
        "--checksums",
        default=mode.checksums_path,
        help=f"Checksum output file (default: {mode.checksums_path})",
    )
    p_update.set_defaults(
        func=lambda a: update_csv(
            csv_path=a.csv,
            image_dir=a.image_dir,
            checksums_path=a.checksums,
            vm_suffix=mode.vm_suffix,
        )
    )

    p_nix = sub.add_parser(
        "generate-nix",
        help="Generate bunnyXX.nix files from rollout.csv",
        description=(
            "Generate bunnyXX.nix files from rollout.csv.\n"
            "This command is intentionally identical in classroom and lockdown mode.\n"
            "It uses all VM rows, including commented rows.\n"
            "Required fields per row: vm, name, email.\n\n"
            "Example:\n"
            f"  {mode.program_name} generate-nix --target-dir hosts/bunnies\n"
        ),
    )
    p_nix.add_argument(
        "--csv",
        default=CLASSROOM_MODE.csv_path,
        help=f"Path to rollout.csv (default: {CLASSROOM_MODE.csv_path}; same in lockdown mode)",
    )
    p_nix.add_argument("--target-dir", required=True, help="Target directory for generated bunnyXX.nix files")
    p_nix.set_defaults(func=lambda a: generate_nix(csv_path=a.csv, target_dir=a.target_dir))

    # The rollout command has its own full parser. It is dispatched before the
    # top-level parser consumes arguments, so rollout-specific options work.
    p_rollout = sub.add_parser(
        "rollout",
        help="Deploy prepared VM images to Windows PCs",
        description=(
            "Deploy prepared VM images to Windows PCs via \\\\PC\\C$.\n"
            "This is the integrated replacement for the old standalone rollout.py.\n\n"
            "Examples:\n"
            f"  {mode.program_name} rollout --dry-run\n"
            f"  {mode.program_name} rollout --src .\n"
            f"  {mode.program_name} rollout --only S40404-14 --src .\n"
            f"  {mode.program_name} rollout --emergency --src .\n"
        ),
    )
    p_rollout.set_defaults(func=lambda _a: run_integrated_rollout([], mode))

    p_help = sub.add_parser(
        "help",
        help="Show general help or command-specific help",
        description=f"Show help. Use '{mode.program_name} help <command>' for command-specific help.",
    )
    p_help.add_argument("topic", nargs="?", help="Optional command name")
    p_help.set_defaults(func=lambda a: _print_help(parser, a.topic, mode))

    return parser


def _print_help(parser: argparse.ArgumentParser, topic: str | None, mode: ModeConfig) -> int:
    if not topic:
        parser.print_help()
        return 0

    if topic == "rollout":
        return run_integrated_rollout(["--help"], mode)

    command_parser = build_parser(mode)
    subparsers_action = next(
        action for action in command_parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    choices = subparsers_action.choices
    if topic not in choices:
        print(f"Unknown help topic: {topic}", file=sys.stderr)
        print(f"Available topics: {', '.join(sorted(choices))}", file=sys.stderr)
        return 2

    choices[topic].print_help()
    return 0


def main(argv: list[str] | None = None, *, mode: ModeConfig = CLASSROOM_MODE) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    parser = build_parser(mode)

    if not args_list:
        parser.print_help()
        return 1

    if args_list[0] in {"-h", "--help"}:
        parser.print_help()
        return 0

    if args_list[0] == "rollout":
        return run_integrated_rollout(args_list[1:], mode)

    args = parser.parse_args(args_list)

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def main_classroom(argv: list[str] | None = None) -> int:
    return main(argv, mode=CLASSROOM_MODE)


def main_lockdown(argv: list[str] | None = None) -> int:
    return main(argv, mode=LOCKDOWN_MODE)
