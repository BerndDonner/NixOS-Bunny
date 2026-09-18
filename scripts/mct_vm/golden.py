from __future__ import annotations

import hashlib
import json
import os
import signal
import shlex
import stat
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

from .config import AppConfig, REPO_ROOT
from .runtime import (
    SHUTDOWN_TIMEOUT,
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
    print(f"Overlay complete: {copied} regular file(s); Continue config intentionally skipped.")


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


def _optimize_image_size(cfg: AppConfig) -> None:
    if not cfg.optimize_image_size:
        return
    print("Optimizing image size...")
    proc = subprocess.run([*ssh_base(cfg.preparation_host_key), "sudo fstrim -av"], check=False)
    if proc.returncode != 0:
        print("WARN: image-size optimization (fstrim) returned a non-zero status", file=sys.stderr)


def _shutdown_external_pid(pid: int, *, key: Path) -> None:
    subprocess.run([*ssh_base(key), "sudo systemctl poweroff"], check=False)
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(1)
    raise TimeoutError(f"QEMU pid {pid} did not exit within {SHUTDOWN_TIMEOUT}s after guest poweroff")



def _stop_external_pid(pid: int) -> None:
    if not pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def prepare_golden(cfg: AppConfig) -> int:
    _validate_golden_files(cfg)

    print("Phase 2a — prepare golden image")
    print(f"  image                 : {cfg.golden_image}")
    print(f"  UEFI state            : {cfg.golden_vars}")
    print(f"  student home content  : {cfg.student_home_content or '(none)'}")
    print(f"  browser start page    : {cfg.browser_start_page}")
    print("  Continue config       : deliberately NOT installed in this step")

    if cfg.run.dry_run:
        print("Dry run: no VM started and no files changed.")
        return 0

    existing = _read_session(cfg)
    if existing is not None:
        raise RuntimeError(
            f"A prepared golden-image QEMU session already appears to be running (pid {existing}). "
            "Finish it with `./scripts/mct-vm.py finalize-golden` or stop it manually."
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

        _configure_browser_start_page(
            start_page=cfg.browser_start_page, key=cfg.preparation_host_key
        )

        if not cfg.golden_vars.is_file():
            raise FileNotFoundError(f"QEMU did not create the expected UEFI state: {cfg.golden_vars}")

        _write_session(cfg, qemu.pid)
        print()
        print("prepare-golden complete. The VM is intentionally left running for manual work.")
        print("Install/test VS Code extensions and Continue now, then run:")
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
    print(f"  Continue config       : {cfg.final_continue_config}")
    print(f"  optimize image size   : {cfg.optimize_image_size}")

    if cfg.run.dry_run:
        print("Dry run: no VM started and no files changed.")
        return 0

    session_pid = _read_session(cfg)
    qemu: subprocess.Popen[bytes] | None = None

    if session_pid is not None:
        print(f"Using the running prepare-golden VM (pid {session_pid}).")
        wait_for_ssh_service(qemu=None)
    else:
        print("No running prepare-golden session found; starting the configured golden image headless.")
        qemu = start_qemu(
            disk=cfg.golden_image,
            vars_file=cfg.golden_vars,
            headless=True,
            discard=True,
        )
        wait_for_ssh_service(qemu=qemu)

    try:
        verify_ssh_login(cfg.preparation_host_key)
        _install_final_continue_config(cfg)
        _optimize_image_size(cfg)
        print("Shutting down golden image cleanly...")
        if qemu is not None:
            poweroff_guest(key=cfg.preparation_host_key, qemu=qemu)
        else:
            assert session_pid is not None
            _shutdown_external_pid(session_pid, key=cfg.preparation_host_key)
        _clear_session()
        print("Golden image finalized successfully.")
        return 0
    except Exception:
        if cfg.run.keep_failed_vm_running:
            pid = qemu.pid if qemu is not None else session_pid
            print(f"Golden VM left running for diagnosis (pid {pid}).", file=sys.stderr)
            if pid is not None:
                _write_session(cfg, pid)
        else:
            if qemu is not None:
                stop_qemu(qemu)
            elif session_pid is not None:
                _stop_external_pid(session_pid)
                _clear_session()
        raise
