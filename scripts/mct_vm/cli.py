from __future__ import annotations

import argparse
import sys

from . import rollout as rollout_module
from .images import clone_images, prepare_images, update_csv
from .individualize import (
    DEFAULT_FORGEJO_URL_TEMPLATE,
    DEFAULT_GITHUB_URL_TEMPLATE,
    DEFAULT_SHUTDOWN_TIMEOUT,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_TIMEOUT,
    IndividualizeOptions,
    individualize_images,
)
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


def _parse_only(values: list[str] | None) -> frozenset[str]:
    result: set[str] = set()
    for value in values or []:
        for item in value.split(","):
            item = item.strip()
            if item:
                result.add(item)
    return frozenset(result)


def _add_individualize_options(parser: argparse.ArgumentParser, mode: ModeConfig) -> None:
    add_common_csv_image_options(parser, mode)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="VM[,VM...]",
        help="Process only selected active VM(s); may be repeated",
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=DEFAULT_SSH_PORT,
        help=f"Host TCP port forwarded to guest SSH (default: {DEFAULT_SSH_PORT})",
    )
    parser.add_argument(
        "--ssh-key",
        help=(
            "Provisioning private key. If omitted, ssh-agent/default identities are used. "
            "For unattended runs, load the key once with ssh-add."
        ),
    )
    parser.add_argument(
        "--ssh-timeout",
        type=int,
        default=DEFAULT_SSH_TIMEOUT,
        help=f"Seconds to wait for guest SSH (default: {DEFAULT_SSH_TIMEOUT})",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=int,
        default=DEFAULT_SHUTDOWN_TIMEOUT,
        help=f"Seconds to wait for QEMU after guest poweroff (default: {DEFAULT_SHUTDOWN_TIMEOUT})",
    )
    parser.add_argument(
        "--qemu-script",
        help="Path to run-qemu.sh (default: scripts/run-qemu.sh next to mct_vm package)",
    )
    parser.add_argument(
        "--logs-dir",
        default="logs",
        help="Directory for phase-3 logs (default: logs)",
    )
    parser.add_argument(
        "--github-url-template",
        default=DEFAULT_GITHUB_URL_TEMPLATE,
        help=(
            "Public bootstrap URL template; placeholders: {repo}, {course}. "
            f"Default: {DEFAULT_GITHUB_URL_TEMPLATE}"
        ),
    )
    parser.add_argument(
        "--forgejo-url-template",
        default=DEFAULT_FORGEJO_URL_TEMPLATE,
        help=(
            "Forgejo origin URL template; only configured, never contacted during phase 3. "
            "Placeholders: {repo}, {course}. "
            f"Default: {DEFAULT_FORGEJO_URL_TEMPLATE}"
        ),
    )
    parser.add_argument(
        "--chrome-start-page",
        default="auto",
        metavar="auto|none|PATH|URL",
        help=(
            "Chrome homepage/startup page. 'auto' finds index.html below ~/reference; "
            "'none' skips Chrome policy (default: auto)."
        ),
    )
    parser.add_argument(
        "--no-vscode-autostart",
        action="store_true",
        help="Do not create KDE autostart entry that opens the student's course folder in VS Code",
    )
    parser.add_argument(
        "--no-trim",
        action="store_true",
        help="Skip final guest fstrim (QEMU discard is enabled by default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate CSV selection and print the plan without starting or modifying VMs",
    )
    parser.add_argument(
        "--keep-on-error",
        action="store_true",
        help="Leave the failed QEMU guest running for manual inspection instead of stopping it",
    )


def _individualize_from_args(args: argparse.Namespace, mode: ModeConfig) -> int:
    options = IndividualizeOptions(
        csv_path=args.csv,
        image_dir=args.image_dir,
        vm_suffix=mode.vm_suffix,
        only_vms=_parse_only(args.only),
        ssh_port=args.ssh_port,
        ssh_key=args.ssh_key,
        ssh_timeout=args.ssh_timeout,
        shutdown_timeout=args.shutdown_timeout,
        qemu_script=args.qemu_script,
        logs_dir=args.logs_dir,
        github_url_template=args.github_url_template,
        forgejo_url_template=args.forgejo_url_template,
        chrome_start_page=args.chrome_start_page,
        vscode_autostart=not args.no_vscode_autostart,
        trim=not args.no_trim,
        dry_run=args.dry_run,
        keep_on_error=args.keep_on_error,
    )
    return individualize_images(options)


def _phase3_from_args(args: argparse.Namespace, mode: ModeConfig) -> int:
    only = _parse_only(args.only)

    if args.dry_run:
        return _individualize_from_args(args, mode)

    clone_rc = clone_images(
        csv_path=args.csv,
        image_dir=args.image_dir,
        golden_qcow2=args.golden_qcow2,
        golden_vars=args.golden_vars,
        vm_suffix=mode.vm_suffix,
        only_vms=only,
    )
    if clone_rc != 0:
        return clone_rc
    return _individualize_from_args(args, mode)


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
            f"  {mode.program_name} generate-nix --target-dir hosts\n"
            + (
                f"  {mode.program_name} phase3\n"
                if mode.name == "classroom"
                else f"  {mode.program_name} clone\n"
            )
            + f"  {mode.program_name} prepare-images\n"
            f"  {mode.program_name} update-csv\n"
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
    p_clone.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="VM[,VM...]",
        help="Clone only selected active VM(s); may be repeated",
    )
    p_clone.set_defaults(
        func=lambda a: clone_images(
            csv_path=a.csv,
            image_dir=a.image_dir,
            golden_qcow2=a.golden_qcow2,
            golden_vars=a.golden_vars,
            vm_suffix=mode.vm_suffix,
            only_vms=_parse_only(a.only),
        )
    )

    if mode.name == "classroom":
        p_individualize = sub.add_parser(
            "individualize",
            help="Fully provision/test/shut down active bunnyXX QCOW2 images over SSH",
            description=(
                "Automate the remaining phase-3 work for already cloned classroom QCOW2 images.\n"
                "For every selected active VM it starts QEMU headless, waits for provisioning SSH,\n"
                "runs nixos-rebuild for .#bunnyXX, configures the course repository without\n"
                "Forgejo credentials, prepares the local student branch, runs _config/setup.sh,\n"
                "sets VS Code course autostart and the Chrome offline-doc start page, validates\n"
                "the result, trims free blocks and powers the guest off cleanly.\n\n"
                "Student branches remain unpublished. The first Forgejo contact is still the\n"
                "student's own `git pub`. Continue configuration is deliberately untouched."
            ),
        )
        _add_individualize_options(p_individualize, mode)
        p_individualize.set_defaults(func=lambda a: _individualize_from_args(a, mode))

        p_phase3 = sub.add_parser(
            "phase3",
            help="Clone missing classroom QCOW2 images and fully individualize them",
            description=(
                "Complete classroom phase 3 in one command: clone missing bunnyXX images from\n"
                "the finished golden QCOW2, then run the full `individualize` workflow.\n"
                "Existing bunnyXX images are intentionally not overwritten. generate-nix is NOT\n"
                "run automatically; host files must already be reviewed/generated."
            ),
        )
        _add_individualize_options(p_phase3, mode)
        p_phase3.add_argument(
            "--golden-qcow2",
            default=mode.golden_qcow2,
            help=f"Golden qcow2 image (default: {mode.golden_qcow2})",
        )
        p_phase3.add_argument(
            "--golden-vars",
            default=mode.golden_vars,
            help=f"Golden OVMF vars file (default: {mode.golden_vars})",
        )
        p_phase3.set_defaults(func=lambda a: _phase3_from_args(a, mode))

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
            "It uses active VM rows only and removes stale bunnyXX.nix files.\n"
            "Required fields per row: vm, course, forgejo, full_name, email.\n\n"
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
