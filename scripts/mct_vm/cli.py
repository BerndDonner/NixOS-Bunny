from __future__ import annotations

import argparse
import sys

from . import rollout as rollout_module
from .images import GOLDEN_QCOW2, GOLDEN_VARS, clone_images, prepare_images, update_csv
from .nixgen import generate_nix


def add_common_csv_image_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--csv", default="rollout.csv", help="Path to rollout.csv (default: rollout.csv)")
    parser.add_argument(
        "--image-dir",
        default=".",
        help="Directory containing VM image files (default: current directory)",
    )


def run_integrated_rollout(argv: list[str]) -> int:
    try:
        return int(rollout_module.main(argv))
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mct-vm",
        formatter_class=argparse.RawTextHelpFormatter,
        description=(
            "mct-vm — MCT VM image and rollout helper\n\n"
            "Active rows are rows in rollout.csv whose first column is not commented with #.\n\n"
            "Typical workflow:\n"
            "  mct-vm clone\n"
            "  # boot each VM and run the matching nixos-rebuild inside it\n"
            "  mct-vm prepare-images\n"
            "  mct-vm update-csv\n"
            "  mct-vm generate-nix --target-dir <path>\n"
            "  mct-vm rollout --dry-run\n"
            "  mct-vm rollout\n"
        ),
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_clone = sub.add_parser(
        "clone",
        help="Clone golden.qcow2 and golden OVMF vars for active VMs",
        description=(
            "Clone golden.qcow2 and golden.OVMF_VARS.fd for active VMs from rollout.csv.\n"
            "Existing target files are skipped with a warning."
        ),
    )
    add_common_csv_image_options(p_clone)
    p_clone.add_argument(
        "--golden-qcow2",
        default=GOLDEN_QCOW2,
        help=f"Golden qcow2 image (default: {GOLDEN_QCOW2})",
    )
    p_clone.add_argument(
        "--golden-vars",
        default=GOLDEN_VARS,
        help=f"Golden OVMF vars file (default: {GOLDEN_VARS})",
    )
    p_clone.set_defaults(
        func=lambda a: clone_images(
            csv_path=a.csv,
            image_dir=a.image_dir,
            golden_qcow2=a.golden_qcow2,
            golden_vars=a.golden_vars,
        )
    )

    p_prepare = sub.add_parser(
        "prepare-images",
        help="Convert active qcow2 images to vmdk and compress them with zstd",
        description=(
            "Run the last image preparation step before rollout.\n"
            "For every active VM from rollout.csv:\n"
            "  1) bunnyXX.qcow2 -> bunnyXX.vmdk\n"
            "  2) bunnyXX.vmdk  -> bunnyXX.vmdk.zst\n\n"
            "Existing files are skipped with a warning."
        ),
    )
    add_common_csv_image_options(p_prepare)
    p_prepare.set_defaults(func=lambda a: prepare_images(csv_path=a.csv, image_dir=a.image_dir))

    p_update = sub.add_parser(
        "update-csv",
        help="Update file and sha256 columns in rollout.csv for active VMs",
        description=(
            "Update rollout.csv after image preparation.\n"
            "For every active VM:\n"
            "  file   = bunnyXX.vmdk.zst\n"
            "  sha256 = SHA256 of the compressed image\n\n"
            "Also writes checksums.sha256 for the active compressed images."
        ),
    )
    add_common_csv_image_options(p_update)
    p_update.add_argument(
        "--checksums",
        default="checksums.sha256",
        help="Checksum output file (default: checksums.sha256)",
    )
    p_update.set_defaults(
        func=lambda a: update_csv(
            csv_path=a.csv,
            image_dir=a.image_dir,
            checksums_path=a.checksums,
        )
    )

    p_nix = sub.add_parser(
        "generate-nix",
        help="Generate bunnyXX.nix files from rollout.csv",
        description=(
            "Generate bunnyXX.nix files from rollout.csv.\n"
            "This command uses all VM rows, including commented rows.\n"
            "Required fields per row: vm, name, email.\n\n"
            "Example:\n"
            "  mct-vm generate-nix --target-dir hosts/bunnies\n"
        ),
    )
    p_nix.add_argument("--csv", default="rollout.csv", help="Path to rollout.csv (default: rollout.csv)")
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
            "  mct-vm rollout --dry-run\n"
            "  mct-vm rollout --src .\n"
            "  mct-vm rollout --only S40404-14 --src .\n"
            "  mct-vm rollout --emergency --src .\n"
        ),
    )
    p_rollout.set_defaults(func=lambda _a: run_integrated_rollout([]))

    p_help = sub.add_parser(
        "help",
        help="Show general help or command-specific help",
        description="Show help. Use 'mct-vm help <command>' for command-specific help.",
    )
    p_help.add_argument("topic", nargs="?", help="Optional command name")
    p_help.set_defaults(func=lambda a: _print_help(parser, a.topic))

    return parser


def _print_help(parser: argparse.ArgumentParser, topic: str | None) -> int:
    if not topic:
        parser.print_help()
        return 0

    if topic == "rollout":
        return run_integrated_rollout(["--help"])

    command_parser = build_parser()
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


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    parser = build_parser()

    if not args_list:
        parser.print_help()
        return 1

    if args_list[0] in {"-h", "--help"}:
        parser.print_help()
        return 0

    if args_list[0] == "rollout":
        return run_integrated_rollout(args_list[1:])

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
