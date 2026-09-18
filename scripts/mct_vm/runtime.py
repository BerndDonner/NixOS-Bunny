from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path


SSH_HOST = "127.0.0.1"
SSH_PORT = 2222
SSH_USER = "student"
SSH_KEY = Path.home() / ".ssh" / "bernd_tracy"
SSH_READY_TIMEOUT = 240
SHUTDOWN_TIMEOUT = 120


def need_cmd(name: str) -> None:
    if shutil.which(name) is None:
        raise FileNotFoundError(f"Missing required command in PATH: {name}")


def qemu_script_path() -> Path:
    path = Path(__file__).resolve().parents[1] / "run-qemu.sh"
    if not path.is_file():
        raise FileNotFoundError(f"QEMU helper not found: {path}")
    if not (path.stat().st_mode & 0o111):
        raise PermissionError(f"QEMU helper is not executable: {path}")
    return path


def ensure_provisioning_key() -> Path:
    if not SSH_KEY.is_file():
        raise FileNotFoundError(
            f"Provisioning private key not found: {SSH_KEY}. "
            "Bunny expects the matching public key."
        )
    pub = SSH_KEY.with_suffix(SSH_KEY.suffix + ".pub") if SSH_KEY.suffix else Path(str(SSH_KEY) + ".pub")
    if not pub.is_file():
        # The private key is sufficient for SSH, so this is only informational.
        print(f"WARN: provisioning public-key file not found: {pub}")
    return SSH_KEY


def ssh_base() -> list[str]:
    key = ensure_provisioning_key()
    return [
        "ssh",
        "-p",
        str(SSH_PORT),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-i",
        str(key),
        f"{SSH_USER}@{SSH_HOST}",
    ]


def check_ssh_port_free() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((SSH_HOST, SSH_PORT))
        except OSError as exc:
            raise RuntimeError(
                f"TCP port {SSH_HOST}:{SSH_PORT} is already in use. "
                "A QEMU provisioning VM may already be running."
            ) from exc


def _read_ssh_banner_once(timeout: float = 2.0) -> str:
    try:
        with socket.create_connection((SSH_HOST, SSH_PORT), timeout=timeout) as sock:
            sock.settimeout(timeout)
            data = b""
            while len(data) < 512 and b"\n" not in data:
                chunk = sock.recv(512 - len(data))
                if not chunk:
                    break
                data += chunk
            return data.decode("ascii", errors="replace").strip()
    except (OSError, TimeoutError):
        return ""


def wait_for_ssh_service(*, qemu: subprocess.Popen[bytes] | None, timeout: int = SSH_READY_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    last_banner = ""
    while time.monotonic() < deadline:
        if qemu is not None:
            rc = qemu.poll()
            if rc is not None:
                raise RuntimeError(f"QEMU exited before SSH became available (exit {rc})")
        last_banner = _read_ssh_banner_once()
        if last_banner.startswith("SSH-"):
            return
        time.sleep(2)
    detail = f" Last banner data: {last_banner!r}" if last_banner else ""
    raise TimeoutError(f"Guest SSH did not become available within {timeout}s.{detail}")


def verify_ssh_login() -> None:
    need_cmd("ssh")
    proc = subprocess.run([*ssh_base(), "true"], check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Provisioning SSH login failed with {SSH_KEY}. "
            "The key may be wrong, encrypted without an available passphrase, or not authorized in Bunny."
        )


def remote_script_command(args: list[str]) -> list[str]:
    quoted = " ".join(shlex.quote(arg) for arg in args)
    return [*ssh_base(), f"bash -s -- {quoted}"]


def start_qemu(
    *,
    disk: Path,
    vars_file: Path,
    headless: bool,
    discard: bool,
    stdout=None,
) -> subprocess.Popen[bytes]:
    check_ssh_port_free()
    cmd = [str(qemu_script_path())]
    if headless:
        cmd.append("--headless")
    if discard:
        cmd.append("--discard")
    cmd.extend(["--ssh", "--port", str(SSH_PORT), "--vars", str(vars_file), str(disk)])
    return subprocess.Popen(cmd, stdout=stdout, stderr=subprocess.STDOUT if stdout is not None else None)


def stop_qemu(qemu: subprocess.Popen[bytes]) -> None:
    if qemu.poll() is not None:
        return
    qemu.terminate()
    try:
        qemu.wait(timeout=10)
    except subprocess.TimeoutExpired:
        qemu.kill()
        qemu.wait(timeout=10)


def poweroff_guest(*, qemu: subprocess.Popen[bytes], timeout: int = SHUTDOWN_TIMEOUT) -> None:
    subprocess.run([*ssh_base(), "sudo systemctl poweroff"], check=False)
    try:
        qemu.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_qemu(qemu)
        raise TimeoutError(
            f"QEMU did not exit within {timeout}s after guest poweroff; process was terminated"
        )


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
