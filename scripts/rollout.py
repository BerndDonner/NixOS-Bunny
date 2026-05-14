#!/usr/bin/env python3
r"""
MCT VM Rollout (VMware) - Python edition

CSV format (7 columns, header allowed):
  pc,vm,login,fullname,email,file,sha256

Where:
  - file   = e.g. bunny00.vmdk.zst
  - sha256 = expected SHA256 of the *compressed* file (.vmdk.zst)

NORMAL MODE (default)
---------------------
Goal: minimal network load + verifiable compressed artifact + remote unpack.

1) Copy <file>.vmdk.zst to \\PC\C$\Virtual_Machines\ using robocopy
2) Verify SHA256 ON THE TARGET (no 4GB read over SMB), best-effort ladder:
     a) schtasks + PowerShell Get-FileHash
     b) schtasks + certutil
     c) local certutil against UNC path (expensive, reads file over network)
3) Write marker file on the remote share: <remote_file>.sha256 (only for .zst)
4) Ensure tools\zstd.exe exists on the target
5) Remote unpack via schtasks as SYSTEM:
      C:\Virtual_Machines\<file>.vmdk.zst -> C:\Virtual_Machines\<vm>.vmdk
   The resulting .vmdk is NOT verified (as requested).

EMERGENCY MODE (--emergency)
----------------------------
Goal: get a startable .vmdk onto each PC even if remote execution / hashing is not acceptable.

- No SHA256 over the network (no remote hash, no UNC hash).
- No marker files (nothing that claims "verified").
- Copy .vmdk.zst to target (unverified, just for "state completeness"/later use).
- Locally unpack .vmdk from the source .vmdk.zst, compute local SHA256 reference of the .vmdk,
  write a manifest CSV row in logdir, then copy the .vmdk to target (unverified).

Manifest is written ONLY in emergency mode (CSV, UTF-8 BOM for Excel).

Assumptions:
- You have admin access from the rollout machine to \\PC\C$ (UNC admin share).
- schtasks remote calls work in normal mode (RPC/task scheduler). WinRM is NOT required.
- Windows 11 targets (PowerShell + certutil present by default).

"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Optional, Dict, Iterator, Tuple, List

_HEX64_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


# ------------------------
# Logging / helpers
# ------------------------

def _now_ts() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(level: str, msg: str, *, logfile: Optional[str] = None) -> None:
    line = f"[{_now_ts()}] {level.upper():5s} {msg}"
    print(line)
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8", errors="replace") as f:
                f.write(line + "\n")
        except OSError:
            pass


def make_logfile(logdir: str) -> str:
    os.makedirs(logdir, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(logdir, f"rollout-{ts}.log")


def ensure_dir(path: str, *, dry_run: bool, logfile: Optional[str]) -> None:
    if dry_run:
        log("INFO", f"[dry-run] mkdir {path}", logfile=logfile)
        return
    os.makedirs(path, exist_ok=True)


def ping_host(pc: str, *, timeout_ms: int, logfile: Optional[str]) -> bool:
    # Only a fast "skip offline host" heuristic.
    p = subprocess.run(
        ["ping", "-n", "1", "-w", str(int(timeout_ms)), pc],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    ok = (p.returncode == 0)
    if not ok:
        log("WARN", f"Ping failed: {pc} (skipping)", logfile=logfile)
        return False
    return True


def robocopy_one(
    src_dir: str,
    dst_dir: str,
    filename: str,
    *,
    retries: int,
    dry_run: bool,
    logfile: Optional[str],
) -> None:
    if dry_run:
        log("INFO", f"[dry-run] robocopy {src_dir} {dst_dir} {filename}", logfile=logfile)
        return
    cmd = [
        "robocopy", src_dir, dst_dir, filename,
        f"/R:{max(0, int(retries))}", "/W:2",
        "/NFL", "/NDL", "/NP", "/NJH", "/NJS",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    rc = p.returncode
    # Robocopy uses bitmask exit codes; >= 8 indicates a failure.
    if rc >= 8:
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        snippet = "\n".join([x for x in [out, err] if x][:8])
        raise RuntimeError(f"robocopy failed rc={rc}\n{snippet}")


def normalize_sha(s: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", s).lower()


def read_text_file(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def write_text_file(path: str, content: str, *, dry_run: bool, logfile: Optional[str]) -> None:
    if dry_run:
        log("INFO", f"[dry-run] write {path}", logfile=logfile)
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(content)


def iter_csv_rows(csv_path: str) -> Iterator[Tuple[int, List[str]]]:
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for idx, row in enumerate(reader, start=1):
            if not row:
                continue
            if row and row[0].strip().startswith("#"):
                continue
            # Header allowed. Accept common first-column names.
            if idx == 1 and row and row[0].strip().lower() in {"pc", "pcname", "host", "computer"}:
                continue
            yield idx, row


def _win_join(a: str, b: str) -> str:
    a = a.rstrip("\\/")
    b = b.lstrip("\\/")
    return a + "\\" + b


def _to_unc(pc: str, target_dir: str) -> str:
    td = target_dir.replace("/", "\\")
    td_rel = td[3:] if len(td) >= 3 and td[1:3] == ":\\" else td.lstrip("\\")
    return rf"\\{pc}\C$\{td_rel}"


# ------------------------
# schtasks helpers
# ------------------------

def _schtasks(
    pc: str,
    args: List[str],
    *,
    dry_run: bool,
    logfile: Optional[str],
) -> subprocess.CompletedProcess[str]:
    cmd = ["schtasks", "/S", pc] + args
    if dry_run:
        log("INFO", "[dry-run] " + " ".join(cmd), logfile=logfile)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        snippet = "\n".join([x for x in [out, err] if x][:12])
        raise RuntimeError(f"schtasks failed rc={p.returncode}\n{snippet}")
    return p


def _normalize_last_run_time(s: str) -> str:
    t = (s or "").strip()
    if not t:
        return ""
    tl = t.lower()
    if tl in {"n/a", "na", "none"}:
        return ""
    if "nie" in tl or "never" in tl or "not run" in tl:
        return ""
    return t


def _get_task_info(pc: str, task_name: str, *, dry_run: bool, logfile: Optional[str]) -> Dict[str, str]:
    p = _schtasks(
        pc,
        ["/Query", "/TN", task_name, "/FO", "CSV", "/V"],
        dry_run=dry_run,
        logfile=logfile,
    )
    if dry_run:
        return {"last run time": "", "last run result": "", "status": ""}

    text = (p.stdout or "").strip()
    if not text:
        return {}

    reader = csv.reader(text.splitlines())
    rows = list(reader)
    if len(rows) < 2:
        return {}

    header = [h.strip().lower() for h in rows[0]]
    data = rows[1]
    if len(data) < len(header):
        data = data + [""] * (len(header) - len(data))

    raw = {header[i]: (data[i].strip() if i < len(data) else "") for i in range(len(header))}

    def pick(*keys: str) -> str:
        for k in keys:
            v = raw.get(k.lower(), "")
            if v:
                return v
        return ""

    last_run_time = pick("last run time", "letzte laufzeit", "letzter lauf", "letzter start")
    last_run_result = pick("last result", "last run result", "letztes ergebnis", "letztes resultat")
    status = pick("status", "zustand")

    return {
        "last run time": _normalize_last_run_time(last_run_time),
        "last run result": last_run_result.strip(),
        "status": status.strip(),
    }


def _parse_task_result(s: str) -> Optional[int]:
    s = (s or "").strip().lower()
    if not s:
        return None
    try:
        if s.startswith("0x"):
            return int(s, 16)
        if s.isdigit():
            return int(s, 10)
        return None
    except ValueError:
        return None


def _sanitize_task_component(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "X"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def _task_debug_dump(pc: str, task_name: str, *, logfile: Optional[str]) -> str:
    try:
        p = _schtasks(pc, ["/Query", "/TN", task_name, "/FO", "LIST", "/V"], dry_run=False, logfile=logfile)
        return (p.stdout or "").strip()
    except Exception as e:
        return f"(could not query task details: {e})"


def _wait_task_done(
    *,
    pc: str,
    task_name: str,
    timeout_sec: int,
    poll_sec: float,
    logfile: Optional[str],
) -> None:
    deadline = time.time() + max(1, int(timeout_sec))
    while True:
        if time.time() > deadline:
            dump = _task_debug_dump(pc, task_name, logfile=logfile)
            raise RuntimeError(f"Timeout waiting for task on {pc}: {task_name}\n{dump}")

        info = _get_task_info(pc, task_name, dry_run=False, logfile=logfile)
        res = _parse_task_result((info.get("last run result", "") or "").strip().lower())
        last_run_time = (info.get("last run time", "") or "").strip()

        if res == 0x41301:
            time.sleep(max(0.2, float(poll_sec)))
            continue

        if res == 0 and last_run_time:
            return

        if res is not None and res != 0:
            dump = _task_debug_dump(pc, task_name, logfile=logfile)
            raise RuntimeError(
                f"Task failed on {pc}: {task_name} (Last Run Result={info.get('last run result','?')})\n{dump}"
            )

        time.sleep(max(0.2, float(poll_sec)))


# ------------------------
# Hashing (normal mode)
# ------------------------

def certutil_sha256_unc(path: str, *, dry_run: bool) -> str:
    """
    Expensive fallback: run certutil locally against a UNC path (reads file over the network).
    """
    if dry_run:
        return "0" * 64
    cmd = ["certutil", "-hashfile", path, "SHA256"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"certutil failed rc={p.returncode}: {(p.stderr or '').strip()}")
    for line in (p.stdout or "").splitlines():
        s = line.strip().replace(" ", "")
        if _HEX64_RE.match(s):
            return s.lower()
    raise RuntimeError("certutil output did not contain a 64-hex SHA256 line")


def remote_sha256_via_schtasks_powershell(
    *,
    pc: str,
    target_dir: str,
    filename: str,
    timeout_sec: int,
    poll_sec: float,
    dry_run: bool,
    logfile: Optional[str],
) -> str:
    """
    Run PowerShell Get-FileHash on the target via schtasks and write only the hex to a sidecar file.
    """
    if dry_run:
        return "0" * 64

    td = target_dir.replace("/", "\\").rstrip("\\")
    in_path = _win_join(td, filename)
    out_name = filename + ".sha256.remote"
    out_path = _win_join(td, out_name)

    safe = _sanitize_task_component(filename)
    task_name = f"MCT_Rollout_HashPS_{safe}_{time.time_ns()}"

    ps_in = in_path.replace("'", "''")
    ps_out = out_path.replace("'", "''")
    tr = (
        r'powershell -NoProfile -ExecutionPolicy Bypass -Command '
        r'"(Get-FileHash -Algorithm SHA256 -LiteralPath ''{infile}'').Hash '
        r'| Out-File -FilePath ''{outfile}'' -Encoding ASCII -Force"'
    ).format(infile=ps_in, outfile=ps_out)

    log("INFO", f"Remote SHA256 (PS Get-FileHash): {pc} ({in_path})", logfile=logfile)

    _schtasks(
        pc,
        [
            "/Create",
            "/TN", task_name,
            "/TR", tr,
            "/SC", "ONCE",
            "/ST", "00:00",
            "/SD", "01/01/2099",
            "/RU", "SYSTEM",
            "/RL", "HIGHEST",
            "/F",
        ],
        dry_run=False,
        logfile=logfile,
    )

    try:
        _schtasks(pc, ["/Run", "/TN", task_name], dry_run=False, logfile=logfile)
        time.sleep(0.5)
        _wait_task_done(pc=pc, task_name=task_name, timeout_sec=timeout_sec, poll_sec=poll_sec, logfile=logfile)

        unc_out = _win_join(_to_unc(pc, target_dir), out_name)
        txt = read_text_file(unc_out)
        if txt is None:
            raise RuntimeError(f"Remote hash output file not found/readable: {unc_out}")

        got = normalize_sha(txt)
        if len(got) != 64:
            raise RuntimeError(f"Remote hash output invalid (not 64-hex): {unc_out} -> {txt!r}")

        # Best-effort remove sidecar
        try:
            os.remove(unc_out)
        except OSError:
            pass

        return got

    finally:
        try:
            _schtasks(pc, ["/Delete", "/TN", task_name, "/F"], dry_run=False, logfile=logfile)
        except Exception:
            pass


def remote_sha256_via_schtasks_certutil(
    *,
    pc: str,
    target_dir: str,
    filename: str,
    timeout_sec: int,
    poll_sec: float,
    dry_run: bool,
    logfile: Optional[str],
) -> str:
    """
    Run certutil on the target via schtasks and redirect its output to a sidecar file, then parse it.
    No PowerShell dependency.
    """
    if dry_run:
        return "0" * 64

    td = target_dir.replace("/", "\\").rstrip("\\")
    in_path = _win_join(td, filename)
    out_name = filename + ".sha256.remote"
    out_path = _win_join(td, out_name)

    safe = _sanitize_task_component(filename)
    task_name = f"MCT_Rollout_HashCU_{safe}_{time.time_ns()}"

    tr = r'cmd.exe /c "certutil -hashfile "{infile}" SHA256 > "{outfile}""'.format(
        infile=in_path,
        outfile=out_path,
    )

    log("INFO", f"Remote SHA256 (certutil): {pc} ({in_path})", logfile=logfile)

    _schtasks(
        pc,
        [
            "/Create",
            "/TN", task_name,
            "/TR", tr,
            "/SC", "ONCE",
            "/ST", "00:00",
            "/SD", "01/01/2099",
            "/RU", "SYSTEM",
            "/RL", "HIGHEST",
            "/F",
        ],
        dry_run=False,
        logfile=logfile,
    )

    try:
        _schtasks(pc, ["/Run", "/TN", task_name], dry_run=False, logfile=logfile)
        time.sleep(0.5)
        _wait_task_done(pc=pc, task_name=task_name, timeout_sec=timeout_sec, poll_sec=poll_sec, logfile=logfile)

        unc_out = _win_join(_to_unc(pc, target_dir), out_name)
        txt = read_text_file(unc_out)
        if txt is None:
            raise RuntimeError(f"Remote certutil output file not found/readable: {unc_out}")

        got = ""
        for line in txt.splitlines():
            s = line.strip().replace(" ", "")
            if _HEX64_RE.match(s):
                got = s.lower()
                break
        if len(got) != 64:
            raise RuntimeError(f"Remote certutil output did not contain a 64-hex line: {unc_out}")

        # Best-effort remove sidecar
        try:
            os.remove(unc_out)
        except OSError:
            pass

        return got

    finally:
        try:
            _schtasks(pc, ["/Delete", "/TN", task_name, "/F"], dry_run=False, logfile=logfile)
        except Exception:
            pass


def remote_sha256_best_effort(
    *,
    pc: str,
    target_dir: str,
    filename: str,
    unc_path: str,
    timeout_sec: int,
    poll_sec: float,
    dry_run: bool,
    logfile: Optional[str],
) -> str:
    """
    Best-effort remote SHA256 with robust fallbacks:
      1) PS Get-FileHash
      2) certutil on target
      3) local certutil on UNC (expensive)
    """
    if dry_run:
        return "0" * 64

    try:
        return remote_sha256_via_schtasks_powershell(
            pc=pc,
            target_dir=target_dir,
            filename=filename,
            timeout_sec=timeout_sec,
            poll_sec=poll_sec,
            dry_run=False,
            logfile=logfile,
        )
    except Exception as e:
        log("WARN", f"Remote hash via PowerShell failed, trying certutil: {e}", logfile=logfile)

    try:
        return remote_sha256_via_schtasks_certutil(
            pc=pc,
            target_dir=target_dir,
            filename=filename,
            timeout_sec=timeout_sec,
            poll_sec=poll_sec,
            dry_run=False,
            logfile=logfile,
        )
    except Exception as e:
        log("WARN", f"Remote hash via certutil failed, falling back to UNC hash (expensive): {e}", logfile=logfile)

    return certutil_sha256_unc(unc_path, dry_run=False)


# ------------------------
# Remote unpack (normal mode)
# ------------------------

def _copy_zstd_tool(*, pc: str, tools_dir: str, unc_tools: str, retries: int, dry_run: bool, logfile: Optional[str]) -> None:
    zstd_local = os.path.join(tools_dir, "zstd.exe")
    if not os.path.exists(zstd_local) and not dry_run:
        raise FileNotFoundError(f"Missing local zstd.exe: {zstd_local}")
    log("INFO", f"Copy zstd.exe -> {pc}: {unc_tools}\\zstd.exe", logfile=logfile)
    robocopy_one(tools_dir, unc_tools, "zstd.exe", retries=retries, dry_run=dry_run, logfile=logfile)


def remote_unpack_via_schtasks(
    *,
    pc: str,
    target_dir: str,
    vm: str,
    zst_filename: str,
    timeout_sec: int,
    poll_sec: float,
    dry_run: bool,
    logfile: Optional[str],
) -> None:
    if dry_run:
        log("INFO", f"[dry-run] remote unpack via schtasks: {pc} ({zst_filename} -> {vm}.vmdk)", logfile=logfile)
        return

    td = target_dir.replace("/", "\\").rstrip("\\")
    in_zst = _win_join(td, zst_filename)
    out_tmp = _win_join(td, f"{vm}.vmdk.tmp")
    out_vmdk = _win_join(td, f"{vm}.vmdk")
    zstd_exe = _win_join(_win_join(td, "tools"), "zstd.exe")

    safe_vm = _sanitize_task_component(vm)
    task_name = f"MCT_Rollout_Unpack_{safe_vm}_{time.time_ns()}"

    tr = (
        r'cmd.exe /c ""{zstd}" -d -f "{inzst}" -o "{tmp}" && move /y "{tmp}" "{final}""'
    ).format(zstd=zstd_exe, inzst=in_zst, tmp=out_tmp, final=out_vmdk)

    log("INFO", f"Remote unpack via schtasks: {pc} ({in_zst} -> {out_vmdk})", logfile=logfile)

    _schtasks(
        pc,
        [
            "/Create",
            "/TN", task_name,
            "/TR", tr,
            "/SC", "ONCE",
            "/ST", "00:00",
            "/SD", "01/01/2099",
            "/RU", "SYSTEM",
            "/RL", "HIGHEST",
            "/F",
        ],
        dry_run=False,
        logfile=logfile,
    )

    try:
        _schtasks(pc, ["/Run", "/TN", task_name], dry_run=False, logfile=logfile)
        time.sleep(0.5)
        _wait_task_done(pc=pc, task_name=task_name, timeout_sec=timeout_sec, poll_sec=poll_sec, logfile=logfile)
        log("INFO", f"Remote unpack OK: {pc} -> {vm}.vmdk", logfile=logfile)
    finally:
        try:
            _schtasks(pc, ["/Delete", "/TN", task_name, "/F"], dry_run=False, logfile=logfile)
        except Exception:
            pass


# ------------------------
# Emergency mode helpers
# ------------------------

def sha256_local_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def append_emergency_manifest(
    manifest_path: str,
    row: Dict[str, str],
    *,
    dry_run: bool,
    logfile: Optional[str],
) -> None:
    headers = [
        "ts",
        "pc",
        "vm",
        "vmdk_sha256_ref",
        "local_vmdk_path",
        "unc_vmdk_path",
        "note",
        "error",
    ]
    if dry_run:
        log("INFO", f"[dry-run] append manifest row: {manifest_path} -> {row}", logfile=logfile)
        return

    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    new_file = not os.path.exists(manifest_path)

    # UTF-8 with BOM so Excel is happy
    with open(manifest_path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in headers})


def local_unpack_vmdk(
    *,
    tools_dir: str,
    src_zst_path: str,
    out_vmdk_path: str,
    dry_run: bool,
    logfile: Optional[str],
) -> None:
    zstd_exe = os.path.join(tools_dir, "zstd.exe")
    if not os.path.exists(zstd_exe) and not dry_run:
        raise FileNotFoundError(f"Missing local zstd.exe for emergency unpack: {zstd_exe}")

    if dry_run:
        log("INFO", f"[dry-run] local unpack: {zstd_exe} -d -f {src_zst_path} -o {out_vmdk_path}", logfile=logfile)
        return

    os.makedirs(os.path.dirname(out_vmdk_path), exist_ok=True)
    cmd = [zstd_exe, "-d", "-f", src_zst_path, "-o", out_vmdk_path]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        snippet = "\n".join([x for x in [out, err] if x][:12])
        raise RuntimeError(f"local zstd unpack failed rc={p.returncode}\n{snippet}")


# ------------------------
# Deploy one row
# ------------------------

def deploy_one(
    *,
    pc: str,
    vm: str,
    filename: str,
    expected_sha: str,
    src_dir: str,
    tools_dir: str,
    target_dir: str,
    retries: int,
    ping_timeout_ms: int,
    force: bool,
    dry_run: bool,
    debug: bool,
    marker_ext: str,
    unpack_timeout_sec: int,
    unpack_poll_sec: float,
    emergency: bool,
    emergency_manifest: Optional[str],
    logfile: Optional[str],
) -> None:
    unc_target = _to_unc(pc, target_dir)
    unc_tools = _win_join(unc_target, "tools")
    unc_zst = _win_join(unc_target, filename)
    marker_path = unc_zst + marker_ext
    unc_vmdk = _win_join(unc_target, f"{vm}.vmdk")

    exp = normalize_sha(expected_sha)
    if len(exp) != 64:
        raise ValueError(f"Expected SHA is not 64-hex: {expected_sha}")

    log("INFO", f"Deploy {pc} -> {vm} ({filename})", logfile=logfile)
    if debug:
        log("DEBUG", f"UNC_TARGET={unc_target}", logfile=logfile)
        log("DEBUG", f"UNC_ZST={unc_zst}", logfile=logfile)
        log("DEBUG", f"UNC_VMDK={unc_vmdk}", logfile=logfile)
        log("DEBUG", f"MODE={'EMERGENCY' if emergency else 'NORMAL'}", logfile=logfile)

    if not ping_host(pc, timeout_ms=ping_timeout_ms, logfile=logfile):
        return

    ensure_dir(unc_target, dry_run=dry_run, logfile=logfile)
    ensure_dir(unc_tools, dry_run=dry_run, logfile=logfile)

    local_zst = os.path.join(src_dir, filename)
    if not os.path.exists(local_zst):
        raise FileNotFoundError(f"Local file not found: {local_zst}")

    # ------------------
    # EMERGENCY
    # ------------------
    if emergency:
        if not emergency_manifest:
            raise RuntimeError("Internal error: emergency_manifest missing in emergency mode")

        # 1) Copy .zst (unverified)
        log("WARN", f"EMERGENCY: copy .zst unverified: {filename}", logfile=logfile)
        robocopy_one(src_dir, unc_target, filename, retries=retries, dry_run=dry_run, logfile=logfile)

        # 2) Local unpack -> temp .vmdk
        tmpdir = tempfile.mkdtemp(prefix=f"mct-emergency-{vm}-")
        local_vmdk = os.path.join(tmpdir, f"{vm}.vmdk")

        try:
            log("WARN", f"EMERGENCY: local unpack -> {local_vmdk}", logfile=logfile)
            local_unpack_vmdk(tools_dir=tools_dir, src_zst_path=local_zst, out_vmdk_path=local_vmdk, dry_run=dry_run, logfile=logfile)

            # 3) Local reference hash (pre-copy). No network hashing.
            if dry_run:
                vmdk_ref = "0" * 64
            else:
                log("WARN", "EMERGENCY: computing local SHA256 reference of .vmdk (pre-copy)", logfile=logfile)
                vmdk_ref = sha256_local_file(local_vmdk)

            log("WARN", f"EMERGENCY: VMDK_REF_SHA256={vmdk_ref}", logfile=logfile)

            # 4) Copy .vmdk (unverified)
            log("WARN", f"EMERGENCY: copy .vmdk unverified: {os.path.basename(local_vmdk)} -> {unc_vmdk}", logfile=logfile)
            robocopy_one(tmpdir, unc_target, os.path.basename(local_vmdk), retries=retries, dry_run=dry_run, logfile=logfile)

            # 5) Manifest row
            append_emergency_manifest(
                emergency_manifest,
                {
                    "ts": _now_ts(),
                    "pc": pc,
                    "vm": vm,
                    "vmdk_sha256_ref": vmdk_ref,
                    "local_vmdk_path": local_vmdk,
                    "unc_vmdk_path": unc_vmdk,
                    "note": "EMERGENCY_UNVERIFIED_COPY",
                    "error": "",
                },
                dry_run=dry_run,
                logfile=logfile,
            )

        except Exception as e:
            try:
                append_emergency_manifest(
                    emergency_manifest,
                    {
                        "ts": _now_ts(),
                        "pc": pc,
                        "vm": vm,
                        "vmdk_sha256_ref": "",
                        "local_vmdk_path": local_vmdk,
                        "unc_vmdk_path": unc_vmdk,
                        "note": "EMERGENCY_FAILED",
                        "error": str(e),
                    },
                    dry_run=dry_run,
                    logfile=logfile,
                )
            except Exception:
                pass
            raise
        finally:
            if not dry_run:
                try:
                    for root, _dirs, files in os.walk(tmpdir, topdown=False):
                        for fn in files:
                            try:
                                os.remove(os.path.join(root, fn))
                            except OSError:
                                pass
                    try:
                        os.rmdir(tmpdir)
                    except OSError:
                        pass
                except Exception:
                    pass

        return

    # ------------------
    # NORMAL
    # ------------------

    # 0) Ensure zstd.exe present on target
    _copy_zstd_tool(pc=pc, tools_dir=tools_dir, unc_tools=unc_tools, retries=retries, dry_run=dry_run, logfile=logfile)

    # Skip logic: marker for .zst only
    if not force:
        marker_txt = read_text_file(marker_path)
        if marker_txt is not None:
            got_marker = normalize_sha(marker_txt)
            if got_marker == exp and os.path.exists(unc_zst):
                log("INFO", f"Up-to-date (marker match), skipping .zst copy: {unc_zst}", logfile=logfile)
                # Ensure vmdk exists; if not, unpack now
                if os.path.exists(unc_vmdk):
                    if debug:
                        log("DEBUG", f"Remote .vmdk exists, skipping unpack: {unc_vmdk}", logfile=logfile)
                    return
                remote_unpack_via_schtasks(
                    pc=pc,
                    target_dir=target_dir,
                    vm=vm,
                    zst_filename=filename,
                    timeout_sec=unpack_timeout_sec,
                    poll_sec=unpack_poll_sec,
                    dry_run=dry_run,
                    logfile=logfile,
                )
                return

    # 1) Copy .zst
    robocopy_one(src_dir, unc_target, filename, retries=retries, dry_run=dry_run, logfile=logfile)

    # 2) Verify .zst hash with best-effort ladder
    got = remote_sha256_best_effort(
        pc=pc,
        target_dir=target_dir,
        filename=filename,
        unc_path=unc_zst,
        timeout_sec=max(60, int(unpack_timeout_sec)),
        poll_sec=unpack_poll_sec,
        dry_run=dry_run,
        logfile=logfile,
    )

    if got != exp:
        raise RuntimeError(f"SHA mismatch for .zst (exp={exp} got={got})")

    log("INFO", "SHA OK (.zst)", logfile=logfile)

    # 3) Write marker
    write_text_file(marker_path, got + "\n", dry_run=dry_run, logfile=logfile)

    # 4) Remote unpack
    remote_unpack_via_schtasks(
        pc=pc,
        target_dir=target_dir,
        vm=vm,
        zst_filename=filename,
        timeout_sec=unpack_timeout_sec,
        poll_sec=unpack_poll_sec,
        dry_run=dry_run,
        logfile=logfile,
    )


# ------------------------
# CLI
# ------------------------

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="rollout.py",
        formatter_class=argparse.RawTextHelpFormatter,
        description=(
            "Roll out VMware VM images (.vmdk.zst) to remote Windows PCs via \\\\PC\\C$.\n"
            "Default: remote SHA256 (best-effort) + marker + remote unpack via schtasks.\n"
            "Emergency: local unpack + local vmdk reference SHA + unverified copies.\n"
        ),
    )
    p.add_argument("--csv", default="rollout.csv", help="Path to rollout.csv (default: rollout.csv in CWD)")
    p.add_argument("--src", default="images", help="Directory containing .vmdk.zst files (default: images)")
    p.add_argument("--tools", default="tools", help="Tools directory (must contain zstd.exe)")
    p.add_argument("--target-dir", default=r"C:\Virtual_Machines", help=r"Target directory on remote C: (default: C:\Virtual_Machines)")
    p.add_argument("--only", dest="only_pc", default="", help="Only deploy rows matching this PC name (case-insensitive)")
    p.add_argument("--force", action="store_true", help="Force overwrite even if marker matches (normal mode only)")
    p.add_argument("--dry-run", action="store_true", help="Print actions but do not change anything")
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    p.add_argument("--retries", type=int, default=2, help="Robocopy retries (default: 2)")
    p.add_argument("--ping-timeout-ms", type=int, default=800, help="Ping timeout in ms (default: 800)")
    p.add_argument("--logdir", default="logs", help="Log directory (default: logs)")
    p.add_argument("--marker-ext", default=".sha256", help="Marker file extension (default: .sha256)")

    p.add_argument(
        "--emergency",
        action="store_true",
        help="Emergency mode: no network hashing, no markers; local unpack + local vmdk reference SHA + unverified copy",
    )

    p.add_argument("--unpack-timeout-sec", type=int, default=1800, help="Remote unpack timeout seconds (default: 1800)")
    p.add_argument("--unpack-poll-sec", type=float, default=2.0, help="Remote task poll interval seconds (default: 2.0)")
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    logfile = make_logfile(args.logdir)

    log("INFO", "=== Rollout started ===", logfile=logfile)
    log("INFO", f"SCRIPT={os.path.abspath(sys.argv[0])}  CWD={os.getcwd()}", logfile=logfile)
    log("INFO", f"CSV={os.path.abspath(args.csv)}  SRC={os.path.abspath(args.src)}  TOOLS={os.path.abspath(args.tools)}", logfile=logfile)
    log(
        "INFO",
        f"TARGET={args.target_dir} RETRIES={args.retries} DRY_RUN={int(args.dry_run)} "
        f"DEBUG={int(args.debug)} ONLY_PC={args.only_pc or '-'} EMERGENCY={int(args.emergency)}",
        logfile=logfile,
    )

    csv_path = os.path.abspath(args.csv)
    src_dir = os.path.abspath(args.src)
    tools_dir = os.path.abspath(args.tools)

    if not os.path.exists(csv_path):
        log("ERROR", f"CSV not found: {csv_path}", logfile=logfile)
        return 2
    if not os.path.isdir(src_dir):
        log("ERROR", f"SRC dir not found: {src_dir}", logfile=logfile)
        return 2

    # zstd is required in both modes:
    # - normal: needs to be staged to the target for remote unpack
    # - emergency: needs to unpack locally
    zstd_local = os.path.join(tools_dir, "zstd.exe")
    if not args.dry_run and not os.path.exists(zstd_local):
        log("ERROR", f"Missing required tool: {zstd_local}", logfile=logfile)
        return 2

    emergency_manifest = None
    if args.emergency:
        ts = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        emergency_manifest = os.path.join(args.logdir, f"manifest-{ts}.csv")
        log("WARN", f"EMERGENCY MODE enabled. Manifest: {os.path.abspath(emergency_manifest)}", logfile=logfile)

    only = (args.only_pc or "").strip().lower()
    failures = 0
    matched = 0

    try:
        for line_no, row in iter_csv_rows(csv_path):
            while len(row) < 7:
                row.append("")
            pc, vm, _login, _fullname, _email, filename, sha = [x.strip() for x in row[:7]]

            if not pc or not vm or not filename or not sha:
                continue
            if only and pc.strip().lower() != only:
                continue

            matched += 1

            try:
                deploy_one(
                    pc=pc,
                    vm=vm,
                    filename=filename,
                    expected_sha=sha,
                    src_dir=src_dir,
                    tools_dir=tools_dir,
                    target_dir=args.target_dir,
                    retries=args.retries,
                    ping_timeout_ms=args.ping_timeout_ms,
                    force=args.force,
                    dry_run=args.dry_run,
                    debug=args.debug,
                    marker_ext=args.marker_ext,
                    unpack_timeout_sec=args.unpack_timeout_sec,
                    unpack_poll_sec=args.unpack_poll_sec,
                    emergency=args.emergency,
                    emergency_manifest=emergency_manifest,
                    logfile=logfile,
                )
            except Exception as e:
                failures += 1
                log("ERROR", f"Line {line_no}: {pc},{vm}: {e}", logfile=logfile)

    except (OSError, csv.Error, UnicodeError) as e:
        log("ERROR", f"CSV read/parse failed: {e}", logfile=logfile)
        log("INFO", f"Logfile: {os.path.abspath(logfile)}", logfile=logfile)
        print(f'Log: "{os.path.abspath(logfile)}"')
        return 2

    if only and matched == 0:
        log("WARN", f"No CSV entries matched --only {args.only_pc!r}", logfile=logfile)

    if failures:
        log("ERROR", f"=== Rollout finished with {failures} error(s) ===", logfile=logfile)
        log("INFO", f"Logfile: {os.path.abspath(logfile)}", logfile=logfile)
        if emergency_manifest:
            log("INFO", f"Emergency manifest: {os.path.abspath(emergency_manifest)}", logfile=logfile)
        print(f'Log: "{os.path.abspath(logfile)}"')
        return 1

    log("INFO", "=== Rollout finished ===", logfile=logfile)
    log("INFO", f"Logfile: {os.path.abspath(logfile)}", logfile=logfile)
    if emergency_manifest:
        log("INFO", f"Emergency manifest: {os.path.abspath(emergency_manifest)}", logfile=logfile)
    print(f'Log: "{os.path.abspath(logfile)}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
