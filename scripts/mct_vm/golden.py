from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

from .config import AppConfig, REPO_ROOT
from .runtime import (
    pid_alive,
    poweroff_guest,
    ssh_base,
    start_qemu,
    stop_qemu,
    verify_ssh_login,
    wait_for_ssh_service,
)

SESSION_DIR = REPO_ROOT / ".mct-vm"
SESSION_FILE = SESSION_DIR / "golden-session.json"


def _copy_qcow2(src: Path, dst: Path) -> None:
    subprocess.run(
        ["cp", "--reflink=auto", "--sparse=always", str(src), str(dst)],
        check=True,
    )
    # Nix store paths are read-only. GNU cp preserves those mode bits when
    # creating the destination, but QEMU needs to open the working QCOW2
    # read/write. Keep the copied image private while restoring owner write.
    dst.chmod(dst.stat().st_mode | stat.S_IWUSR)


def _copy_plain(src: Path, dst: Path) -> None:
    subprocess.run(["cp", "--reflink=auto", str(src), str(dst)], check=True)
    # UEFI VARS files are mutable VM state and must always be writable.
    dst.chmod(dst.stat().st_mode | stat.S_IWUSR)


def _pair_state(image: Path, vars_file: Path) -> str:
    have_image = image.is_file()
    have_vars = vars_file.is_file()
    if have_image and have_vars:
        return "complete"
    if not have_image and not have_vars:
        return "missing"
    return "partial"


def _require_no_partial_pair(image: Path, vars_file: Path, *, label: str) -> str:
    state = _pair_state(image, vars_file)
    if state == "partial":
        raise RuntimeError(
            f"Inconsistent {label}: expected image and UEFI state together:\n"
            f"  {image}\n  {vars_file}"
        )
    return state


def _session_payload(*, image: Path, vars_file: Path, pid: int) -> dict[str, object]:
    return {
        "pid": pid,
        "image": str(image),
        "vars": str(vars_file),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }


def _write_session(*, image: Path, vars_file: Path, pid: int) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(
        json.dumps(_session_payload(image=image, vars_file=vars_file, pid=pid), indent=2) + "\n",
        encoding="utf-8",
    )


def _read_live_session() -> tuple[int, Path, Path] | None:
    if not SESSION_FILE.is_file():
        return None
    try:
        payload = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        image = Path(str(payload["image"])).resolve()
        vars_file = Path(str(payload["vars"])).resolve()
    except Exception:
        return None
    if not pid_alive(pid):
        return None
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return None
    if "qemu-system" not in cmdline or str(image) not in cmdline:
        return None
    return pid, image, vars_file


def _clear_session() -> None:
    SESSION_FILE.unlink(missing_ok=True)


def ensure_no_live_golden_session() -> None:
    session = _read_live_session()
    if session is None:
        return
    pid, image, _vars = session
    raise RuntimeError(
        f"A golden-image QEMU session is still running (pid {pid}, image {image}). "
        "Shut it down cleanly before changing golden artifacts."
    )


def _copy_home_overlay(source_root: Path, *, key: Path) -> None:
    if not source_root.is_dir():
        raise FileNotFoundError(f"student_home_content is not a directory: {source_root}")

    print(f"Overlaying student home content from {source_root}")
    proc = subprocess.Popen(
        [*ssh_base(key), 'tar --no-same-owner -C "$HOME" -xf -'],
        stdin=subprocess.PIPE,
    )
    assert proc.stdin is not None

    copied = 0
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tf:
            for path in sorted(source_root.rglob("*")):
                rel = path.relative_to(source_root)
                if rel.as_posix() == ".continue/config.yaml":
                    continue
                try:
                    st = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                tf.add(path, arcname=rel.as_posix(), recursive=False)
                copied += 1
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass

    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"student home overlay failed (ssh/tar exit {rc})")
    print(f"Overlay complete: {copied} regular file(s); overlay Continue config intentionally skipped.")


def _guest_path(path: str) -> str:
    if path.startswith("~/"):
        return "/home/student/" + path[2:]
    if path.startswith("/"):
        return path
    raise ValueError(
        "[golden_image].browser_start_page must be an absolute guest path or start with '~/'."
    )


def _configure_browser_start_page(*, start_page: str, key: Path) -> None:
    guest_path = _guest_path(start_page)
    script = r'''set -euo pipefail
fail() { echo "ERROR: $*" >&2; exit 1; }

start_page=$1
[[ -f "$start_page" ]] || fail "configured browser start page is missing: $start_page"
start_page=$(realpath "$start_page")
url="file://$start_page"
policy_tmp=$(mktemp)
jq -n --arg url "$url" '{
    HomepageLocation: $url,
    HomepageIsNewTabPage: false,
    ShowHomeButton: true,
    RestoreOnStartup: 4,
    RestoreOnStartupURLs: [$url]
}' > "$policy_tmp"
sudo install -Dm0644 "$policy_tmp" /etc/chromium/policies/managed/mct-classroom.json
rm -f "$policy_tmp"
echo "Chromium start page: $url"
'''
    proc = subprocess.run(
        [*ssh_base(key), f"bash -s -- {shlex.quote(guest_path)}"],
        input=script,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Could not configure Chromium start page: {guest_path}")


def _install_final_continue_config(cfg: AppConfig) -> None:
    source = cfg.final_continue_config
    if not source.is_file():
        raise FileNotFoundError(f"Final Continue configuration not found: {source}")

    content = source.read_bytes()
    local_sha = hashlib.sha256(content).hexdigest()
    cmd = [
        *ssh_base(cfg.preparation_host_key),
        'mkdir -p "$HOME/.continue" && cat > "$HOME/.continue/config.yaml" && '
        'chmod 0644 "$HOME/.continue/config.yaml"',
    ]
    proc = subprocess.run(cmd, input=content, check=False)
    if proc.returncode != 0:
        raise RuntimeError("Failed to install final Continue configuration")

    verify = subprocess.run(
        [*ssh_base(cfg.preparation_host_key), 'sha256sum "$HOME/.continue/config.yaml" | cut -d" " -f1'],
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    remote_sha = (verify.stdout or "").strip()
    if verify.returncode != 0 or remote_sha != local_sha:
        raise RuntimeError(
            f"Continue config verification failed: local={local_sha}, remote={remote_sha or '<none>'}"
        )
    print("Final Continue configuration installed and verified.")


def _clean_manual_user_traces(cfg: AppConfig) -> None:
    script = r'''set -euo pipefail

if pgrep -u "$USER" -f '(chromium|/chromium)( |$)' >/dev/null 2>&1; then
    echo "ERROR: Chromium is running; refusing to clean a live profile" >&2
    exit 1
fi

rm -f -- "$HOME/.bash_history"
sudo rm -f -- /root/.bash_history

profile="$HOME/.config/chromium/Default"
if [[ -d "$profile" ]]; then
    rm -f -- \
        "$profile/History" "$profile/History-journal" \
        "$profile/Login Data" "$profile/Login Data-journal" \
        "$profile/Login Data For Account" "$profile/Login Data For Account-journal" \
        "$profile/Cookies" "$profile/Cookies-journal" \
        "$profile/Network/Cookies" "$profile/Network/Cookies-journal" \
        "$profile/Visited Links" "$profile/Top Sites" "$profile/Top Sites-journal" \
        "$profile/Shortcuts" "$profile/Shortcuts-journal" \
        "$profile/Media History" "$profile/Media History-journal" \
        "$profile/Current Session" "$profile/Current Tabs" \
        "$profile/Last Session" "$profile/Last Tabs"

    rm -rf -- \
        "$profile/Sessions" "$profile/Session Storage" "$profile/Local Storage" \
        "$profile/IndexedDB" "$profile/Service Worker" "$profile/WebStorage" \
        "$profile/Shared Dictionary" "$profile/Cache" "$profile/Code Cache" \
        "$profile/GPUCache"
fi

rm -rf -- "$HOME/.cache/chromium"
echo "Manual traces cleaned; Chromium preferences/bookmarks/extensions preserved."
'''
    proc = subprocess.run(
        [*ssh_base(cfg.preparation_host_key), "bash -s"],
        input=script,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("Could not clean Bash/Chromium traces from the golden image")


def _optimize_image_size(cfg: AppConfig) -> None:
    if not cfg.optimize_image_size:
        return
    print("Optimizing image size...")
    proc = subprocess.run([*ssh_base(cfg.preparation_host_key), "sudo fstrim -av"], check=False)
    if proc.returncode != 0:
        print("WARN: image-size optimization (fstrim) returned a non-zero status", file=sys.stderr)


def _find_built_qcow2(out_paths: list[Path]) -> Path:
    candidates: list[Path] = []
    for out in out_paths:
        if out.is_file() and out.suffix == ".qcow2":
            candidates.append(out)
        elif out.is_dir():
            candidates.extend(path for path in out.rglob("*.qcow2") if path.is_file())
    unique = sorted({path.resolve() for path in candidates})
    if len(unique) != 1:
        rendered = "\n  ".join(str(path) for path in unique) or "<none>"
        raise RuntimeError(
            "nix build must produce exactly one QCOW2 artifact; found:\n  " + rendered
        )
    return unique[0]


def _nix_build_generic_qcow2() -> Path:
    if shutil.which("nix") is None:
        raise FileNotFoundError("Missing required command in PATH: nix")
    cmd = ["nix", "build", ".#qcow2", "--no-link", "--print-out-paths"]
    print("$ " + shlex.join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        check=True,
    )
    out_paths = [Path(line.strip()) for line in (proc.stdout or "").splitlines() if line.strip()]
    if not out_paths:
        raise RuntimeError("nix build returned no output path")
    return _find_built_qcow2(out_paths)


def build_golden(cfg: AppConfig) -> int:
    """Build and automatically prepare a new golden image for manual GUI work.

    The command intentionally leaves QEMU running and keeps the disk under a
    ``.building`` name. The manual golden becomes the stable
    ``golden-*.qcow2`` only when ``finalize-golden`` starts after a clean manual
    shutdown.
    """

    ensure_no_live_golden_session()
    base_state = _require_no_partial_pair(cfg.golden_image, cfg.golden_vars, label="manual golden")
    building_state = _require_no_partial_pair(
        cfg.golden_building_image, cfg.golden_building_vars, label="building golden"
    )
    finalized_state = _require_no_partial_pair(
        cfg.golden_finalized_image, cfg.golden_finalized_vars, label="finalized golden"
    )
    finalizing_state = _require_no_partial_pair(
        cfg.golden_finalizing_image, cfg.golden_finalizing_vars, label="finalizing golden"
    )

    if base_state == "complete" and building_state == "complete":
        raise RuntimeError(
            "Both the stable manual golden and a .building golden exist. "
            "Refusing to guess which manual lineage to use. Resolve this with reset-golden."
        )
    if base_state == "complete":
        print(f"Manual golden already exists; build-golden skips it: {cfg.golden_image}")
        print("Use reset-golden if you intentionally want to start the golden workflow from scratch.")
        return 0
    if finalized_state == "complete" or finalizing_state == "complete":
        raise RuntimeError(
            "A finalized/finalizing golden exists but the protected manual golden is missing. "
            "Refusing to start a second golden lineage beside it. Use reset-golden to resolve this explicitly."
        )
    if building_state == "complete":
        print(f"A golden work image already exists: {cfg.golden_building_image}")
        print("Continue the manual work if needed, shut it down cleanly, then run finalize-golden.")
        print("Use reset-golden if this work image should be discarded.")
        return 0

    print("Build golden image and prepare it for manual work")
    print(f"  work image            : {cfg.golden_building_image}")
    print(f"  work UEFI state       : {cfg.golden_building_vars}")
    print(f"  student home content  : {cfg.student_home_content or '(none)'}")
    print("  browser start page    : deliberately deferred to finalize-golden")
    print(f"  Continue config       : {cfg.final_continue_config}")
    print("  Arduino USB passthrough: enabled for manual hardware test")

    if cfg.run.dry_run:
        print("Dry run: would run `nix build .#qcow2`, copy its QCOW2 to the .building image and start QEMU.")
        return 0

    cfg.vm_images_dir.mkdir(parents=True, exist_ok=True)
    built = _nix_build_generic_qcow2()
    print(f"Copying Nix QCOW2 {built} -> {cfg.golden_building_image}")
    _copy_qcow2(built, cfg.golden_building_image)

    qemu = start_qemu(
        disk=cfg.golden_building_image,
        vars_file=cfg.golden_building_vars,
        headless=False,
        discard=True,
        arduino=True,
    )

    try:
        print("Waiting for provisioning SSH...")
        wait_for_ssh_service(qemu=qemu)
        verify_ssh_login(cfg.preparation_host_key)

        if cfg.student_home_content is not None:
            _copy_home_overlay(cfg.student_home_content, key=cfg.preparation_host_key)
        else:
            print("No student_home_content configured; overlay skipped.")

        _install_final_continue_config(cfg)

        if not cfg.golden_building_vars.is_file():
            raise FileNotFoundError(
                f"QEMU did not create the expected UEFI state: {cfg.golden_building_vars}"
            )

        _write_session(
            image=cfg.golden_building_image,
            vars_file=cfg.golden_building_vars,
            pid=qemu.pid,
        )
        print()
        print("build-golden automatic preparation is complete.")
        print("The VM is intentionally left running for the manual Golden setup.")
        print("When the manual work is finished, shut the VM down cleanly, then run:")
        print("  ./scripts/mct-vm.py finalize-golden")
        return 0
    except Exception:
        if cfg.run.keep_failed_vm_running:
            print(f"Golden VM left running for diagnosis (pid {qemu.pid}).", file=sys.stderr)
            _write_session(
                image=cfg.golden_building_image,
                vars_file=cfg.golden_building_vars,
                pid=qemu.pid,
            )
        else:
            stop_qemu(qemu)
            _clear_session()
        raise


def finalize_golden(cfg: AppConfig) -> int:
    """Finalize a powered-off manual golden without ever modifying it in-place."""

    finalized_state = _require_no_partial_pair(
        cfg.golden_finalized_image, cfg.golden_finalized_vars, label="finalized golden"
    )
    if finalized_state == "complete":
        print(f"Finalized golden already exists; skipping: {cfg.golden_finalized_image}")
        print("Use reset-finalized-golden if you intentionally want to finalize the manual golden again.")
        return 0

    ensure_no_live_golden_session()
    _clear_session()

    base_state = _require_no_partial_pair(cfg.golden_image, cfg.golden_vars, label="manual golden")
    building_state = _require_no_partial_pair(
        cfg.golden_building_image, cfg.golden_building_vars, label="building golden"
    )

    if base_state == "missing":
        if building_state != "complete":
            raise FileNotFoundError(
                "No powered-off manual golden is available. Run build-golden first."
            )
        # The manual work is now complete. Publish that valuable source under
        # its stable name before making the finalization copy.
        print(f"Promoting manual work image: {cfg.golden_building_image} -> {cfg.golden_image}")
        if not cfg.run.dry_run:
            cfg.golden_building_image.replace(cfg.golden_image)
            cfg.golden_building_vars.replace(cfg.golden_vars)
    elif building_state == "complete":
        raise RuntimeError(
            "Both the stable manual golden and a .building golden exist. "
            "Refusing to guess which contains the intended manual work. Use reset-golden to resolve this explicitly."
        )

    print("Finalize golden image from a protected copy")
    print(f"  protected manual image: {cfg.golden_image}")
    print(f"  working copy          : {cfg.golden_finalizing_image}")
    print(f"  final output          : {cfg.golden_finalized_image}")
    print("  manual trace cleanup  : Bash history + sensitive Chromium state")
    print(f"  browser start page    : {cfg.browser_start_page}")
    print(f"  optimize image size   : {cfg.optimize_image_size}")

    if cfg.run.dry_run:
        print("Dry run: would copy the manual golden to .finalizing, clean it, shut it down, then rename to .finalized.")
        return 0

    # .finalizing is explicitly transient: never continue from a failed prior
    # attempt. Always start from the protected manual golden.
    cfg.golden_finalizing_image.unlink(missing_ok=True)
    cfg.golden_finalizing_vars.unlink(missing_ok=True)
    _copy_qcow2(cfg.golden_image, cfg.golden_finalizing_image)
    _copy_plain(cfg.golden_vars, cfg.golden_finalizing_vars)

    qemu = start_qemu(
        disk=cfg.golden_finalizing_image,
        vars_file=cfg.golden_finalizing_vars,
        headless=True,
        discard=True,
    )
    try:
        wait_for_ssh_service(qemu=qemu)
        verify_ssh_login(cfg.preparation_host_key)
        _clean_manual_user_traces(cfg)
        _configure_browser_start_page(
            start_page=cfg.browser_start_page,
            key=cfg.preparation_host_key,
        )
        _optimize_image_size(cfg)
        print("Shutting down finalizing golden cleanly...")
        poweroff_guest(key=cfg.preparation_host_key, qemu=qemu)

        cfg.golden_finalizing_image.replace(cfg.golden_finalized_image)
        cfg.golden_finalizing_vars.replace(cfg.golden_finalized_vars)
        _clear_session()
        print(f"Golden image finalized successfully: {cfg.golden_finalized_image}")
        print(f"Protected manual source remains unchanged: {cfg.golden_image}")
        return 0
    except Exception:
        if cfg.run.keep_failed_vm_running:
            print(f"Golden VM left running for diagnosis (pid {qemu.pid}).", file=sys.stderr)
            _write_session(
                image=cfg.golden_finalizing_image,
                vars_file=cfg.golden_finalizing_vars,
                pid=qemu.pid,
            )
        else:
            stop_qemu(qemu)
            _clear_session()
        raise
