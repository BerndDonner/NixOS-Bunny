from __future__ import annotations

import hashlib
import json
import shlex
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


def _session_payload(cfg: AppConfig, pid: int) -> dict[str, object]:
    return {
        "pid": pid,
        "image": str(cfg.golden_image),
        "vars": str(cfg.golden_vars),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }


def _write_session(cfg: AppConfig, pid: int) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(_session_payload(cfg, pid), indent=2) + "\n", encoding="utf-8")


def _read_session(cfg: AppConfig) -> int | None:
    if not SESSION_FILE.is_file():
        return None
    try:
        payload = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        image = Path(str(payload["image"])).resolve()
        vars_file = Path(str(payload["vars"])).resolve()
    except Exception:
        return None
    if image != cfg.golden_image.resolve() or vars_file != cfg.golden_vars.resolve():
        return None
    if not pid_alive(pid):
        return None
    # Avoid trusting a stale PID that has been reused by another process.
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return None
    if "qemu-system" not in cmdline or str(cfg.golden_image) not in cmdline:
        return None
    return pid


def _clear_session() -> None:
    try:
        SESSION_FILE.unlink()
    except FileNotFoundError:
        pass


def _validate_golden_files(cfg: AppConfig) -> None:
    if not cfg.golden_image.is_file():
        raise FileNotFoundError(f"Golden image not found: {cfg.golden_image}")
    # run-qemu.sh can create a missing VARS file, but clone later needs the
    # reviewed persistent state. During phase 2 we therefore expect/create it
    # through run-qemu and verify it afterwards.


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
sudo install -Dm0644 "$policy_tmp" /etc/opt/chrome/policies/managed/mct-classroom.json
rm -f "$policy_tmp"
echo "Chrome start page: $url"
'''
    proc = subprocess.run(
        [*ssh_base(key), f"bash -s -- {shlex.quote(guest_path)}"],
        input=script,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Could not configure Chrome start page: {guest_path}")


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


def _verify_final_continue_config(cfg: AppConfig) -> None:
    source = cfg.final_continue_config
    if not source.is_file():
        raise FileNotFoundError(f"Final Continue configuration not found: {source}")

    local_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    verify = subprocess.run(
        [*ssh_base(cfg.preparation_host_key), 'sha256sum "$HOME/.continue/config.yaml" | cut -d" " -f1'],
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    remote_sha = (verify.stdout or "").strip()
    if verify.returncode != 0 or remote_sha != local_sha:
        raise RuntimeError(
            "Continue config changed or is missing after the manual phase: "
            f"expected={local_sha}, remote={remote_sha or '<none>'}"
        )
    print("Final Continue configuration still matches the reviewed repository copy.")


def _clean_manual_user_traces(cfg: AppConfig) -> None:
    # finalize-golden is run only after the visible/manual VM has been shut down.
    # It then boots the image headless, so there is no Chrome process that can
    # rewrite profile databases after we remove sensitive state. Keep Chrome's
    # Preferences/Secure Preferences/Bookmarks/Extensions intact; remove only
    # history, credentials, sessions/site data and caches from the manual phase.
    script = r'''set -euo pipefail

if pgrep -u "$USER" -f '(google-chrome|google-chrome-stable|/chrome)( |$)' >/dev/null 2>&1; then
    echo "ERROR: Chrome is running; refusing to clean a live profile" >&2
    exit 1
fi

rm -f -- "$HOME/.bash_history"
sudo rm -f -- /root/.bash_history

profile="$HOME/.config/google-chrome/Default"
if [[ -d "$profile" ]]; then
    rm -f -- \
        "$profile/History" \
        "$profile/History-journal" \
        "$profile/Login Data" \
        "$profile/Login Data-journal" \
        "$profile/Login Data For Account" \
        "$profile/Login Data For Account-journal" \
        "$profile/Cookies" \
        "$profile/Cookies-journal" \
        "$profile/Network/Cookies" \
        "$profile/Network/Cookies-journal" \
        "$profile/Visited Links" \
        "$profile/Top Sites" \
        "$profile/Top Sites-journal" \
        "$profile/Shortcuts" \
        "$profile/Shortcuts-journal" \
        "$profile/Media History" \
        "$profile/Media History-journal" \
        "$profile/Current Session" \
        "$profile/Current Tabs" \
        "$profile/Last Session" \
        "$profile/Last Tabs"

    rm -rf -- \
        "$profile/Sessions" \
        "$profile/Session Storage" \
        "$profile/Local Storage" \
        "$profile/IndexedDB" \
        "$profile/Service Worker" \
        "$profile/WebStorage" \
        "$profile/Shared Dictionary" \
        "$profile/Cache" \
        "$profile/Code Cache" \
        "$profile/GPUCache"
fi

rm -rf -- "$HOME/.cache/google-chrome" "$HOME/.cache/google-chrome-stable"

echo "Manual traces cleaned; Chrome preferences/bookmarks/extensions preserved."
'''
    proc = subprocess.run(
        [*ssh_base(cfg.preparation_host_key), "bash -s"],
        input=script,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("Could not clean Bash/Chrome traces from the golden image")


def _optimize_image_size(cfg: AppConfig) -> None:
    if not cfg.optimize_image_size:
        return
    print("Optimizing image size...")
    proc = subprocess.run([*ssh_base(cfg.preparation_host_key), "sudo fstrim -av"], check=False)
    if proc.returncode != 0:
        print("WARN: image-size optimization (fstrim) returned a non-zero status", file=sys.stderr)


def prepare_golden(cfg: AppConfig) -> int:
    _validate_golden_files(cfg)

    print("Phase 2a — prepare golden image")
    print(f"  image                 : {cfg.golden_image}")
    print(f"  UEFI state            : {cfg.golden_vars}")
    print(f"  student home content  : {cfg.student_home_content or '(none)'}")
    print("  browser start page    : deliberately deferred to finalize-golden")
    print(f"  Continue config       : {cfg.final_continue_config}")

    if cfg.run.dry_run:
        print("Dry run: no VM started and no files changed.")
        return 0

    existing = _read_session(cfg)
    if existing is not None:
        raise RuntimeError(
            f"A prepared golden-image QEMU session already appears to be running (pid {existing}). "
            "Shut it down cleanly before starting another prepare-golden run."
        )

    qemu = start_qemu(
        disk=cfg.golden_image,
        vars_file=cfg.golden_vars,
        headless=False,
        discard=True,
    )

    try:
        print("Waiting for provisioning SSH...")
        wait_for_ssh_service(qemu=qemu)
        verify_ssh_login(cfg.preparation_host_key)

        if cfg.student_home_content is not None:
            _copy_home_overlay(cfg.student_home_content, key=cfg.preparation_host_key)
        else:
            print("No student_home_content configured; overlay skipped.")

        # Install the authoritative Continue configuration before the manual
        # phase so Continue can be tested exactly as students will use it.
        # Any .continue/config.yaml in the home overlay is deliberately ignored.
        _install_final_continue_config(cfg)

        if not cfg.golden_vars.is_file():
            raise FileNotFoundError(f"QEMU did not create the expected UEFI state: {cfg.golden_vars}")

        _write_session(cfg, qemu.pid)
        print()
        print("prepare-golden complete. The VM is intentionally left running for manual work.")
        print("Install/test VS Code extensions, Continue and Chrome now.")
        print("When the manual work is finished, shut the VM down cleanly, then run:")
        print("  ./scripts/mct-vm.py finalize-golden")
        return 0
    except Exception:
        if cfg.run.keep_failed_vm_running:
            print(f"Golden VM left running for diagnosis (pid {qemu.pid}).", file=sys.stderr)
            _write_session(cfg, qemu.pid)
        else:
            stop_qemu(qemu)
        raise


def finalize_golden(cfg: AppConfig) -> int:
    _validate_golden_files(cfg)

    print("Phase 2b — finalize golden image")
    print(f"  image                 : {cfg.golden_image}")
    print(f"  Continue config       : {cfg.final_continue_config} (verify only)")
    print("  manual trace cleanup  : Bash history + sensitive Chrome state")
    print(f"  browser start page    : {cfg.browser_start_page}")
    print(f"  optimize image size   : {cfg.optimize_image_size}")

    if cfg.run.dry_run:
        print("Dry run: no VM started and no files changed.")
        return 0

    session_pid = _read_session(cfg)
    if session_pid is not None:
        raise RuntimeError(
            f"The prepare-golden VM is still running (pid {session_pid}). "
            "Shut the VM down cleanly before running finalize-golden. "
            "Finalization deliberately starts from a powered-off manual session "
            "so Bash/Chrome cannot rewrite state during cleanup."
        )

    # A dead prepare-golden process leaves a harmless session marker behind.
    # At this point the visible/manual VM is no longer running, so discard it
    # and boot the reviewed image once, headless, for deterministic cleanup.
    _clear_session()
    print("Starting the powered-off golden image headless for final cleanup.")
    qemu = start_qemu(
        disk=cfg.golden_image,
        vars_file=cfg.golden_vars,
        headless=True,
        discard=True,
    )
    wait_for_ssh_service(qemu=qemu)

    try:
        verify_ssh_login(cfg.preparation_host_key)
        _verify_final_continue_config(cfg)
        _clean_manual_user_traces(cfg)
        # The start page is a managed system policy, so it is installed only
        # after the manual Chrome state has been cleaned. User preferences such
        # as privacy choices, search engine, bookmarks and extensions remain.
        _configure_browser_start_page(
            start_page=cfg.browser_start_page, key=cfg.preparation_host_key
        )
        _optimize_image_size(cfg)
        print("Shutting down golden image cleanly...")
        poweroff_guest(key=cfg.preparation_host_key, qemu=qemu)
        _clear_session()
        print("Golden image finalized successfully.")
        return 0
    except Exception:
        if cfg.run.keep_failed_vm_running:
            print(f"Golden VM left running for diagnosis (pid {qemu.pid}).", file=sys.stderr)
            _write_session(cfg, qemu.pid)
        else:
            stop_qemu(qemu)
            _clear_session()
        raise
