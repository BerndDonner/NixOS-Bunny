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


def ensure_provisioning_key(key: Path) -> Path:
    if not key.is_file():
        raise FileNotFoundError(
            f"Preparation-host private key not found: {key}. "
            "mct-vm never creates or rotates setup keys automatically."
        )
    return key


def _public_key_identity(text: str) -> tuple[str, str]:
    parts = text.strip().split()
    if len(parts) < 2:
        raise ValueError("Invalid OpenSSH public key; expected '<type> <base64> [comment]'")
    return parts[0], parts[1]


def verify_provisioning_key_pair(*, private_key: Path, public_key: Path) -> None:
    """Verify that the preparation-host private key matches Bunny's versioned public key.

    Comments are deliberately ignored; existing golden images may still carry the
    old ``bernd@tracy`` comment while using exactly the same cryptographic key.
    """
    ensure_provisioning_key(private_key)
    if not public_key.is_file():
        raise FileNotFoundError(f"Versioned Bunny setup public key not found: {public_key}")
    need_cmd("ssh-keygen")

    proc = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(private_key)],
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Could not derive a public key from preparation_host_key {private_key}. "
            "Check the key and its passphrase."
        )

    actual = _public_key_identity(proc.stdout or "")
    expected = _public_key_identity(public_key.read_text(encoding="utf-8"))
    if actual != expected:
        raise RuntimeError(
            "preparation_host_key does not match the public setup key built into Bunny: "
            f"{public_key}"
        )


def ssh_base(key: Path) -> list[str]:
    key = ensure_provisioning_key(key)
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
    """Fail only if something is actually listening on the provisioning port.

    Do not probe availability with ``bind()`` here.  After one provisioning VM
    shuts down, TCP connections to its forwarded SSH port may remain in
    ``TIME_WAIT`` briefly.  A plain bind probe can then fail with EADDRINUSE
    even though QEMU itself has already exited and no listener remains.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex((SSH_HOST, SSH_PORT)) == 0:
            raise RuntimeError(
                f"TCP port {SSH_HOST}:{SSH_PORT} already has a listener. "
                "A QEMU provisioning VM may already be running."
            )


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


def verify_ssh_login(key: Path) -> None:
    need_cmd("ssh")
    proc = subprocess.run([*ssh_base(key), "true"], check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Provisioning SSH login failed with {key}. "
            "The key may be wrong, encrypted without an available passphrase, or not authorized in Bunny."
        )


def wait_for_ssh_service_down(
    *, qemu: subprocess.Popen[bytes] | None, timeout: int = 60
) -> None:
    """Wait until the guest has actually stopped serving SSH.

    This is needed around an in-guest reboot. QEMU's host-forwarded port remains
    bound for the lifetime of the QEMU process, so a listening host port alone
    cannot tell us whether the guest is down.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if qemu is not None:
            rc = qemu.poll()
            if rc is not None:
                raise RuntimeError(f"QEMU exited while waiting for guest reboot (exit {rc})")
        if not _read_ssh_banner_once(timeout=1.0).startswith("SSH-"):
            return
        time.sleep(1)
    raise TimeoutError(f"Guest SSH did not go down within {timeout}s after reboot request")


def reboot_guest(
    *,
    key: Path,
    qemu: subprocess.Popen[bytes],
    down_timeout: int = 60,
    up_timeout: int = SSH_READY_TIMEOUT,
) -> None:
    """Reboot the guest and wait for a complete down/up SSH cycle.

    ``nixos-rebuild switch`` can write a new hostname without changing the
    running kernel's hostname. Phase 3 therefore needs a real reboot before
    provisioning and validation continue.
    """
    subprocess.run(
        [*ssh_base(key), "sudo systemctl reboot"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    wait_for_ssh_service_down(qemu=qemu, timeout=down_timeout)
    wait_for_ssh_service(qemu=qemu, timeout=up_timeout)
    verify_ssh_login(key)


def remote_script_command(key: Path, args: list[str]) -> list[str]:
    quoted = " ".join(shlex.quote(arg) for arg in args)
    return [*ssh_base(key), f"bash -s -- {quoted}"]


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


def poweroff_guest(
    *, key: Path, qemu: subprocess.Popen[bytes], timeout: int = SHUTDOWN_TIMEOUT
) -> None:
    subprocess.run([*ssh_base(key), "sudo systemctl poweroff"], check=False)
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
