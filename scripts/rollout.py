#!/usr/bin/env python3
r"""
MCT VM Rollout (VMware) - Python edition

Copies .vmdk.zst images to remote Windows PCs via \\PC\C$ share and verifies SHA256 using certutil
on the UNC path.

CSV format (7 columns, header allowed):
  pc,vm,login,fullname,email,file,sha256

Marker behavior (default):
  - Marker file: <remote_file>.sha256
  - If marker exists and matches expected SHA and image exists -> skip
  - If marker missing but image exists -> compute remote SHA via certutil; if matches -> write marker and skip
  - Else -> copy, verify, write marker

Optional remote unpack (Windows Task Scheduler / schtasks):
  - Enable with: --remote-unpack
  - Copies tools\\zstd.exe to \\PC\C$\<target_dir>\tools\zstd.exe (no hashing/version check)
  - Creates & runs a remote scheduled task as SYSTEM to unpack:
      C:\Virtual_Machines\<file>.vmdk.zst  ->  C:\Virtual_Machines\<vm>.vmdk
    using an intermediate .tmp and an atomic rename.
  - The unpacked .vmdk is NOT verified (as requested).
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import re
import subprocess
import sys
import time
from typing import Optional, Dict, Iterator, Tuple, List

_HEX64_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


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
    # NOTE: We only use ping as a fast "skip offline host" heuristic.
    # Windows ping output encoding may vary; use utf-8 with replacement to avoid crashes.
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


def certutil_sha256(path: str) -> str:
    cmd = ["certutil", "-hashfile", path, "SHA256"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"certutil failed rc={p.returncode}: {(p.stderr or '').strip()}")
    for line in (p.stdout or "").splitlines():
        s = line.strip().replace(" ", "")
        if _HEX64_RE.match(s):
            return s.lower()
    raise RuntimeError("certutil output did not contain a 64-hex SHA256 line")


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


def _parse_schtasks_query(output: str) -> Dict[str, str]:
    info: Dict[str, str] = {}
    for raw in output.splitlines():
        line = raw.rstrip()
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        info[k.strip().lower()] = v.strip()
    return info


def _normalize_last_run_time(s: str) -> str:
    """
    schtasks uses locale-dependent text. Normalize "not run" into empty string.
    """
    t = (s or "").strip()
    if not t:
        return ""
    tl = t.lower()
    if tl in {"n/a", "na", "none"}:
        return ""
    # German/English variants that mean "never"
    if "nie" in tl or "never" in tl or "not run" in tl:
        return ""
    return t


def _get_task_info(pc: str, task_name: str, *, dry_run: bool, logfile: Optional[str]) -> Dict[str, str]:
    p = _schtasks(pc, ["/Query", "/TN", task_name, "/FO", "LIST", "/V"], dry_run=dry_run, logfile=logfile)
    d = _parse_schtasks_query(p.stdout or "")

    # Map locale-dependent keys (German/English) to stable internal keys.
    # "Last Run Time"
    if "letzte laufzeit" in d and "last run time" not in d:
        d["last run time"] = d["letzte laufzeit"].strip()
    # Some variants: "Last Result" -> treat as last run result if present
    if "letztes ergebnis" in d and "last run result" not in d:
        d["last run result"] = d["letztes ergebnis"].strip()
    if "last result" in d and "last run result" not in d:
        d["last run result"] = d["last result"].strip()
    if "last run time" in d:
        d["last run time"] = _normalize_last_run_time(d["last run time"])

    # Keep "status" as-is; value is locale-dependent. We no longer rely on it for completion.
    return d


def _parse_task_result(s: str) -> Optional[int]:
    s = (s or "").strip().lower()
    if not s:
        return None
    # Handles "0", "0x0", "267009", "0x41301"
    try:
        if s.startswith("0x"):
            return int(s, 16)
        if s.isdigit():
            return int(s, 10)
        return None
    except ValueError:
        return None
        

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
    td = target_dir.replace("/", "\\").rstrip("\\")
    in_zst = _win_join(td, zst_filename)
    out_tmp = _win_join(td, f"{vm}.vmdk.tmp")
    out_vmdk = _win_join(td, f"{vm}.vmdk")
    zstd_exe = _win_join(_win_join(td, "tools"), "zstd.exe")

    task_name = f"MCT_Rollout_Unpack_{vm}"

    pre_last_run_time = ""
    try:
        info = _get_task_info(pc, task_name, dry_run=dry_run, logfile=logfile)
        pre_last_run_time = info.get("last run time", "") or ""
    except Exception:
        pre_last_run_time = ""

    tr = (
        r'cmd.exe /c ""{zstd}" -d -f "{inzst}" -o "{tmp}" && move /y "{tmp}" "{final}""'
    ).format(zstd=zstd_exe, inzst=in_zst, tmp=out_tmp, final=out_vmdk)

    log("INFO", f"Remote unpack via schtasks: {pc}  ({in_zst} -> {out_vmdk})", logfile=logfile)

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
        dry_run=dry_run,
        logfile=logfile,
    )

    _schtasks(pc, ["/Run", "/TN", task_name], dry_run=dry_run, logfile=logfile)

    if dry_run:
        log("INFO", f"[dry-run] would wait for task completion and then delete task {task_name!r}", logfile=logfile)
        return

    deadline = time.time() + max(1, int(timeout_sec))
    last_seen_status = ""
    while True:
        if time.time() > deadline:
            # Keep the task for post-mortem (you can query it on the teacher PC).
            raise RuntimeError(f"Timeout waiting for remote unpack task on {pc}: {task_name}")

        info = _get_task_info(pc, task_name, dry_run=dry_run, logfile=logfile)
        last_run_time = (info.get("last run time", "") or "").strip()
        last_run_result = (info.get("last run result", "") or "").strip().lower()
        status = (info.get("status", "") or "").strip()
        if status and status != last_seen_status:
            last_seen_status = status
            # Debug-ish breadcrumb that survives locales; harmless in normal logs.
            log("INFO", f"Remote task status: {pc} {task_name} -> {status}", logfile=logfile)

        res = _parse_task_result(last_run_result)

        ran = False
        if last_run_time:
            if not pre_last_run_time:
                ran = True
            elif last_run_time != pre_last_run_time:
                ran = True

        if ran:
            # 0x41301 (267009) = task is currently running -> keep waiting
            if res == 0x41301:
                time.sleep(max(0.2, float(poll_sec)))
                continue

            # success
            if res == 0:
                break

            # other numeric result -> failure
            if res is not None:
                raise RuntimeError(
                    f"Remote unpack task failed on {pc}: {task_name} "
                    f"(Last Run Result={info.get('last run result','?')})"
                )

        # If we can't decide yet (no run time / no result), keep polling
        time.sleep(max(0.2, float(poll_sec)))

    # Clean up after success.
    _schtasks(pc, ["/Delete", "/TN", task_name, "/F"], dry_run=dry_run, logfile=logfile)
    log("INFO", f"Remote unpack OK: {pc} -> {vm}.vmdk", logfile=logfile)


def _copy_zstd(*, pc: str, tools_dir: str, unc_tools: str, retries: int, dry_run: bool, logfile: Optional[str]) -> None:
    zstd_local = os.path.join(tools_dir, "zstd.exe")
    if not os.path.exists(zstd_local):
        raise FileNotFoundError(f"Missing local zstd.exe: {zstd_local}")
    log("INFO", f"Copy zstd.exe -> {pc}: {unc_tools}\\zstd.exe", logfile=logfile)
    robocopy_one(tools_dir, unc_tools, "zstd.exe", retries=retries, dry_run=dry_run, logfile=logfile)


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
    remote_unpack: bool,
    unpack_timeout_sec: int,
    unpack_poll_sec: float,
    logfile: Optional[str],
) -> None:
    unc_target = _to_unc(pc, target_dir)
    unc_tools = _win_join(unc_target, "tools")
    unc_file = _win_join(unc_target, filename)
    marker_path = unc_file + marker_ext
    unc_vmdk = _win_join(unc_target, f"{vm}.vmdk")

    log("INFO", f"Deploy {pc} -> {vm} ({filename})", logfile=logfile)
    if debug:
        log("DEBUG", f"UNC_TARGET={unc_target}", logfile=logfile)
        log("DEBUG", f"UNC_FILE={unc_file}", logfile=logfile)
        log("DEBUG", f"MARKER={marker_path}", logfile=logfile)
        if remote_unpack:
            log("DEBUG", f"UNC_VMDK={unc_vmdk}", logfile=logfile)

    if not ping_host(pc, timeout_ms=ping_timeout_ms, logfile=logfile):
        return

    ensure_dir(unc_target, dry_run=dry_run, logfile=logfile)
    ensure_dir(unc_tools, dry_run=dry_run, logfile=logfile)

    exp = normalize_sha(expected_sha)
    if len(exp) != 64:
        raise ValueError(f"Expected SHA is not 64-hex: {expected_sha}")

    def maybe_unpack_after_skip() -> None:
        if not remote_unpack:
            return
        if os.path.exists(unc_vmdk):
            if debug:
                log("DEBUG", f"Remote .vmdk exists, skipping unpack: {unc_vmdk}", logfile=logfile)
            return
        _copy_zstd(pc=pc, tools_dir=tools_dir, unc_tools=unc_tools, retries=retries, dry_run=dry_run, logfile=logfile)
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

    if not force:
        marker_txt = read_text_file(marker_path)
        if marker_txt is not None:
            got_marker = normalize_sha(marker_txt)
            if got_marker == exp and os.path.exists(unc_file):
                log("INFO", f"Up-to-date (marker match), skipping copy: {unc_file}", logfile=logfile)
                maybe_unpack_after_skip()
                return

        if os.path.exists(unc_file):
            log("INFO", f"Marker missing/mismatch; verifying remote SHA256 (certutil): {unc_file}", logfile=logfile)
            got = certutil_sha256(unc_file)
            if got == exp:
                log("INFO", "SHA OK (remote file matches). Writing marker and skipping copy.", logfile=logfile)
                write_text_file(marker_path, got + "\n", dry_run=dry_run, logfile=logfile)
                maybe_unpack_after_skip()
                return
            log("WARN", f"Remote SHA mismatch -> will overwrite (exp={exp} got={got})", logfile=logfile)

    local_file = os.path.join(src_dir, filename)
    if not os.path.exists(local_file):
        raise FileNotFoundError(f"Local file not found: {local_file}")

    robocopy_one(src_dir, unc_target, filename, retries=retries, dry_run=dry_run, logfile=logfile)

    log("INFO", f"Verify SHA256 (certutil): {unc_file}", logfile=logfile)
    got = certutil_sha256(unc_file)
    if got != exp:
        raise RuntimeError(f"SHA mismatch after copy (exp={exp} got={got})")

    log("INFO", "SHA OK", logfile=logfile)
    write_text_file(marker_path, got + "\n", dry_run=dry_run, logfile=logfile)

    if remote_unpack:
        _copy_zstd(pc=pc, tools_dir=tools_dir, unc_tools=unc_tools, retries=retries, dry_run=dry_run, logfile=logfile)
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


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="rollout.py",
        formatter_class=argparse.RawTextHelpFormatter,
        description=(
            "Roll out VMware VM images (.vmdk.zst) to remote PCs via \\\\PC\\C$ share.\n"
            "Remote SHA check uses certutil on the UNC path and a marker file to skip unchanged copies.\n"
        ),
        epilog=(
            "Marker logic:\n"
            "  - <remote_file>.sha256 contains the last successfully deployed SHA.\n"
            "  - If marker matches expected SHA and the file exists, rollout skips copying.\n\n"
            "Remote SHA check:\n"
            "  certutil -hashfile \\\\PC\\C$\\... SHA256\n\n"
            "Remote unpack (optional):\n"
            "  - Enable with --remote-unpack\n"
            "  - Copies tools\\zstd.exe to the remote tools directory.\n"
            "  - Runs a scheduled task as SYSTEM on the remote PC to unpack:\n"
            "      C:\\Virtual_Machines\\<file>.vmdk.zst -> C:\\Virtual_Machines\\<vm>.vmdk\n"
        ),
    )
    p.add_argument("--csv", default="rollout.csv", help="Path to rollout.csv (default: rollout.csv in CWD)")
    p.add_argument("--src", default="images", help="Directory containing image files (default: images)")
    p.add_argument("--tools", default="tools", help="Tools directory (must contain zstd.exe if --remote-unpack)")
    p.add_argument("--target-dir", default=r"C:\Virtual_Machines", help=r"Target directory on remote C: (default: C:\Virtual_Machines)")
    p.add_argument("--only", dest="only_pc", default="", help="Only deploy rows matching this PC name (case-insensitive)")
    p.add_argument("--force", action="store_true", help="Force overwrite even if marker matches")
    p.add_argument("--dry-run", action="store_true", help="Print actions but do not change anything")
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    p.add_argument("--retries", type=int, default=2, help="Robocopy retries (default: 2)")
    p.add_argument("--ping-timeout-ms", type=int, default=800, help="Ping timeout in ms (default: 800)")
    p.add_argument("--logdir", default="logs", help="Log directory (default: logs)")
    p.add_argument("--marker-ext", default=".sha256", help="Marker file extension (default: .sha256)")

    p.add_argument("--remote-unpack", action="store_true", help="After copy+SHA, unpack on remote PC via schtasks using tools\\zstd.exe")
    p.add_argument("--unpack-timeout-sec", type=int, default=1800, help="Remote unpack timeout in seconds (default: 1800)")
    p.add_argument("--unpack-poll-sec", type=float, default=2.0, help="Remote task poll interval in seconds (default: 2.0)")
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
        f"DEBUG={int(args.debug)} ONLY_PC={args.only_pc or '-'} REMOTE_UNPACK={int(args.remote_unpack)}",
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
    if args.remote_unpack:
        zstd_local = os.path.join(tools_dir, "zstd.exe")
        if not os.path.exists(zstd_local):
            log("ERROR", f"--remote-unpack requested but missing: {zstd_local}", logfile=logfile)
            return 2

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
                    remote_unpack=args.remote_unpack,
                    unpack_timeout_sec=args.unpack_timeout_sec,
                    unpack_poll_sec=args.unpack_poll_sec,
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
        print(f'Log: "{os.path.abspath(logfile)}"')
        return 1

    log("INFO", "=== Rollout finished ===", logfile=logfile)
    log("INFO", f"Logfile: {os.path.abspath(logfile)}", logfile=logfile)
    print(f'Log: "{os.path.abspath(logfile)}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
