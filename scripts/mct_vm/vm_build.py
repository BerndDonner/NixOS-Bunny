from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .artifacts import image_artifacts
from .config import AppConfig, REPO_ROOT
from .csv_model import CsvRow, read_rollout_csv, require_fields
from .selection import select_rows
from .runtime import (
    check_ssh_port_free,
    poweroff_guest,
    reboot_guest,
    remote_script_command,
    ssh_base,
    start_qemu,
    stop_qemu,
    verify_ssh_login,
    wait_for_ssh_service,
)


def _selected_rows(cfg: AppConfig) -> list[CsvRow]:
    doc = read_rollout_csv(cfg.assignments_file)
    return select_rows(
        doc.active_rows(),
        include=cfg.run.vms_include,
        exclude=cfg.run.vms_exclude,
    )


def _format_url(template: str, *, repo: str, course: str) -> str:
    try:
        return template.format(repo=repo, course=course)
    except KeyError as exc:
        raise ValueError(
            f"Unknown placeholder in URL template {template!r}: {exc}. "
            "Supported placeholders are {repo} and {course}."
        ) from exc


def _copy_qcow2(src: Path, dst: Path) -> None:
    subprocess.run(
        ["cp", "--reflink=auto", "--sparse=always", str(src), str(dst)],
        check=True,
    )


def _copy_plain(src: Path, dst: Path) -> None:
    subprocess.run(["cp", "--reflink=auto", str(src), str(dst)], check=True)


def _append_log(log_path: Path, text: str) -> None:
    with log_path.open("a", encoding="utf-8") as log:
        log.write(text)
        if text and not text.endswith("\n"):
            log.write("\n")


def _run_logged(
    cmd: list[str],
    *,
    log_path: Path,
    input_text: str | None = None,
    check: bool = True,
    echo: bool = True,
) -> subprocess.CompletedProcess[str]:
    _append_log(log_path, "$ " + shlex.join(cmd))
    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = proc.stdout or ""
    _append_log(log_path, output)
    if echo and output:
        print(output, end="" if output.endswith("\n") else "\n")
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=output)
    return proc


def _copy_binary_via_ssh(*, source: Path, remote_path: str, key: Path, log_path: Path) -> None:
    cmd = [*ssh_base(key), f"cat > {shlex.quote(remote_path)}"]
    _append_log(log_path, "$ " + shlex.join(cmd) + f" < {source}")
    with source.open("rb") as src:
        proc = subprocess.run(
            cmd,
            stdin=src,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    output = (proc.stdout or b"").decode(errors="replace")
    _append_log(log_path, output)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=output)


def _classroom_provision_script() -> str:
    return r'''set -euo pipefail

course=$1
student=$2
full_name=$3
email=$4
github_url=$5
forgejo_url=$6

repo="MCT_${course}"
repo_dir="$HOME/$repo"

fail() { echo "ERROR: $*" >&2; exit 1; }
expect_config() {
    key=$1; expected=$2
    actual=$(git config --global --get "$key" 2>/dev/null || true)
    [[ "$actual" == "$expected" ]] || fail "$key: expected '$expected', got '${actual:-<unset>}'"
}

expect_config user.name "$full_name"
expect_config user.email "$email"
expect_config mct.student "$student"
expect_config mct.course "$course"

if [[ -e "$repo_dir" && ! -d "$repo_dir/.git" ]]; then
    fail "$repo_dir exists but is not a Git repository"
fi

if [[ ! -d "$repo_dir/.git" ]]; then
    echo "Cloning public bootstrap mirror: $github_url"
    cloned=0
    for attempt in $(seq 1 10); do
        if git clone -o github "$github_url" "$repo_dir"; then
            cloned=1; break
        fi
        echo "Clone attempt $attempt/10 failed; retrying in 2 seconds..." >&2
        rm -rf "$repo_dir"
        sleep 2
    done
    [[ "$cloned" -eq 1 ]] || fail "could not clone $github_url"
fi

cd "$repo_dir"
if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$forgejo_url"
else
    git remote add origin "$forgejo_url"
fi
if [[ "$(git config --local --get branch.master.remote 2>/dev/null || true)" == "github" ]]; then
    git config --local --unset-all branch.master.remote 2>/dev/null || true
    git config --local --unset-all branch.master.merge 2>/dev/null || true
fi
if git remote get-url github >/dev/null 2>&1; then
    git remote remove github
fi

if grep -Eq '\.continue/config\.yaml|arduino-cli\.yaml' _config/setup.sh; then
    fail "course repository contains an obsolete _config/setup.sh; publish the fixed MCT template first"
fi
[[ ! -e arduino-cli.yaml ]] || fail "obsolete arduino-cli.yaml is still present in the course repository"

if [[ "$student" == "donner" ]]; then
    git switch master
    git config --local branch.master.remote origin
    git config --local branch.master.merge refs/heads/master
else
    if git show-ref --verify --quiet "refs/heads/$student"; then
        git switch "$student"
    else
        git switch -c "$student" master
    fi
    git config --local --unset-all "branch.$student.remote" 2>/dev/null || true
    git config --local --unset-all "branch.$student.merge" 2>/dev/null || true
fi

bash _config/setup.sh
'''


def _classroom_validate_script() -> str:
    return r'''set -euo pipefail

vm=$1
course=$2
student=$3
full_name=$4
email=$5
forgejo_url=$6
repo="MCT_${course}"
repo_dir="$HOME/$repo"
fail() { echo "ERROR: $*" >&2; exit 1; }

[[ "$(hostname)" == "$vm" ]] || fail "hostname is $(hostname), expected $vm"
[[ "$(git config --global --get user.name 2>/dev/null || true)" == "$full_name" ]] || fail "wrong Git user.name"
[[ "$(git config --global --get user.email 2>/dev/null || true)" == "$email" ]] || fail "wrong Git user.email"
[[ "$(git config --global --get mct.student 2>/dev/null || true)" == "$student" ]] || fail "wrong mct.student"
[[ "$(git config --global --get mct.course 2>/dev/null || true)" == "$course" ]] || fail "wrong mct.course"
[[ -d "$HOME/reference" ]] || fail "offline documentation directory $HOME/reference is missing"
command -v code >/dev/null || fail "VS Code command 'code' is missing"
command -v chromium >/dev/null || fail "Chromium command is missing"
[[ -f "$HOME/.continue/config.yaml" ]] || fail "Continue configuration is missing; golden preparation/finalization was not completed"

[[ -d "$repo_dir/.git" ]] || fail "$repo_dir is not a Git repository"
cd "$repo_dir"
expected_branch=$student
[[ "$student" == "donner" ]] && expected_branch=master
[[ "$(git branch --show-current)" == "$expected_branch" ]] || fail "wrong Git branch"
if git remote get-url github >/dev/null 2>&1; then fail "GitHub bootstrap remote is still present"; fi
[[ "$(git remote)" == "origin" ]] || fail "finished repository must contain only the Forgejo origin remote"
[[ "$(git remote get-url origin)" == "$forgejo_url" ]] || fail "wrong origin remote URL"
if git config --local --get-regexp '^branch\..*\.remote$' 2>/dev/null | grep -Eq '[[:space:]]github$'; then
    fail "branch tracking metadata still refers to removed GitHub remote"
fi
[[ "$(git config --local --get core.hooksPath 2>/dev/null || true)" == "_config/hooks" ]] || fail "course hooks are not active"
git config --local --get-all include.path | grep -Fxq '../_config/gitconfig' || fail "course gitconfig include is missing"
[[ -f .vscode/settings.json ]] || fail ".vscode/settings.json is missing"
[[ -f .vscode/launch.json ]] || fail ".vscode/launch.json is missing"

if [[ "$student" == "donner" ]]; then
    [[ "$(git config --local --get branch.master.remote 2>/dev/null || true)" == "origin" ]] || fail "teacher master remote metadata is wrong"
    [[ "$(git config --local --get branch.master.merge 2>/dev/null || true)" == "refs/heads/master" ]] || fail "teacher master merge metadata is wrong"
else
    if git config --local --get "branch.$student.remote" >/dev/null 2>&1; then
        fail "student branch already has an upstream; first publication must remain git pub"
    fi
    grep -Fq '"files.readonlyInclude"' .vscode/settings.json || fail "VS Code read-only protection is missing"
    grep -Fq "$student" .vscode/settings.json || fail "VS Code settings do not contain the student login"
fi

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    git status --short
    fail "versioned course repository files changed during provisioning"
fi

echo "Validation OK: $vm / $course / $expected_branch"
'''


def _lockdown_repo_script() -> str:
    return r'''set -euo pipefail

repo_name=$1
branch=$2
full_name=$3
email=$4
course=$5
student=$6
forgejo_url=$7
bundle=/tmp/mct-exam.bundle
repo_dir="$HOME/$repo_name"
fail() { echo "ERROR: $*" >&2; exit 1; }

[[ "$(git config --global --get user.name 2>/dev/null || true)" == "$full_name" ]] || fail "wrong Git user.name"
[[ "$(git config --global --get user.email 2>/dev/null || true)" == "$email" ]] || fail "wrong Git user.email"
[[ "$(git config --global --get mct.student 2>/dev/null || true)" == "$student" ]] || fail "wrong mct.student"
[[ "$(git config --global --get mct.course 2>/dev/null || true)" == "$course" ]] || fail "wrong mct.course"
[[ -f "$bundle" ]] || fail "exam bundle is missing"
rm -rf -- "$repo_dir"
git clone -b "$branch" "$bundle" "$repo_dir"
git -C "$repo_dir" remote remove origin 2>/dev/null || true
git -C "$repo_dir" remote add origin "$forgejo_url"
rm -f -- "$bundle"
[[ -d "$repo_dir/.git" ]] || fail "exam repository clone failed"
[[ "$(git -C "$repo_dir" branch --show-current)" == "$branch" ]] || fail "wrong exam repository branch"
[[ "$(git -C "$repo_dir" remote)" == "origin" ]] || fail "exam repository must contain only origin"
[[ "$(git -C "$repo_dir" remote get-url origin)" == "$forgejo_url" ]] || fail "wrong exam origin URL"
echo "Exam repository installed: $repo_dir ($branch) -> $forgejo_url"
'''


def _lockdown_finalize_script() -> str:
    return r'''set -euo pipefail

target=$1
expected_hostname=$2
fail() { echo "ERROR: $*" >&2; exit 1; }

echo "Switching to final lockdown generation: $target"
sudo nixos-rebuild switch --flake "path:/home/student/NixOS-Bunny#$target"

# Keep only the current system generation so the bootloader cannot be used to
# select an older classroom generation without the exam firewall.
sudo nix-env --profile /nix/var/nix/profiles/system --delete-generations old
sudo /run/current-system/bin/switch-to-configuration boot

generation_count=$(sudo nix-env --profile /nix/var/nix/profiles/system --list-generations | sed '/^[[:space:]]*$/d' | wc -l)
[[ "$generation_count" -eq 1 ]] || fail "expected exactly one NixOS system generation, found $generation_count"
boot_entry_count=$(find /boot/loader/entries -maxdepth 1 -type f -name 'nixos-generation-*.conf' 2>/dev/null | wc -l)
[[ "$boot_entry_count" -eq 1 ]] || fail "expected exactly one bootable NixOS generation entry, found $boot_entry_count"
[[ "$(cat /etc/hostname)" == "$expected_hostname" ]] || fail "/etc/hostname is not $expected_hostname"
systemctl is-enabled --quiet mct-exam-firewall || fail "mct-exam-firewall is not enabled"
systemctl is-active --quiet mct-exam-firewall || fail "mct-exam-firewall is not active"

if command -v nft >/dev/null 2>&1; then
    sudo nft list table inet mct_exam >/dev/null || fail "mct_exam nftables table is missing"
fi

sudo fstrim -av || true
echo "LOCKDOWN_FINALIZATION_OK"
sudo systemctl poweroff
'''


def _prepare_lockdown_bundle(cfg: AppConfig, temp_dir: Path) -> tuple[Path, str, str]:
    repo = cfg.lockdown_repo
    if repo is None:
        raise ValueError("[lockdown].repo is empty; configure the local exam repository first")
    if not repo.is_dir() or not (repo / ".git").exists():
        raise FileNotFoundError(f"Configured lockdown repository is not a Git repository: {repo}")

    top = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.strip()
    if Path(top).resolve() != repo.resolve():
        raise ValueError(f"[lockdown].repo must point at the Git repository root: {repo}")

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout
    if status.strip():
        raise RuntimeError(
            f"Lockdown repository has uncommitted/untracked files: {repo}. "
            "Commit the exact exam state before building images."
        )

    branch = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    ).stdout.strip()
    if not branch:
        raise RuntimeError(f"Lockdown repository must have a named current branch (not detached HEAD): {repo}")

    bundle = temp_dir / f"{repo.name}.bundle"
    subprocess.run(["git", "-C", str(repo), "bundle", "create", str(bundle), "--all"], check=True)
    subprocess.run(["git", "-C", str(repo), "bundle", "verify", str(bundle)], check=True, stdout=subprocess.DEVNULL)
    return bundle, repo.name, branch


def _describe_row(cfg: AppConfig, row: CsvRow) -> None:
    course = row.raw["course"].strip()
    student = row.raw["forgejo"].strip()
    artifacts = image_artifacts(row.vm, cfg.vm_suffix)
    print(f"{row.vm}:")
    print(f"  output    : {artifacts.qcow2(cfg.vm_images_dir)}")
    print(f"  identity  : {row.raw['full_name']} <{row.raw['email']}> / {student}")
    print(f"  course    : {course}")
    if cfg.mode == "classroom":
        repo = f"MCT_{course}"
        github_url = _format_url(cfg.course_public_source, repo=repo, course=course)
        forgejo_url = _format_url(cfg.course_student_origin, repo=repo, course=course)
        print(f"  repository: {repo}")
        print(f"  source    : {github_url}")
        print(f"  origin    : {forgejo_url} (configured only; no login/push)")
        print(f"  branch    : {'master' if student == 'donner' else student}")
    else:
        exam = cfg.lockdown_repo.name if cfg.lockdown_repo else "<unconfigured>"
        exam_remote = f"https://{cfg.forgejo_host}/{cfg.forgejo_exam_owner}/{exam}_{student}.git"
        print(f"  repository: {cfg.lockdown_repo or '(not configured)'} (local Git repo)")
        print(f"  origin    : {exam_remote} (configured only; no login/push)")
        print(f"  final Nix : .#{row.vm}-lockdown")


def _final_pair_state(disk: Path, vars_file: Path) -> str:
    if disk.is_file() and vars_file.is_file():
        return "complete"
    if not disk.exists() and not vars_file.exists():
        return "missing"
    return "partial"


def build_vms(cfg: AppConfig) -> int:
    rows = _selected_rows(cfg)
    if not rows:
        print(f"WARN: no VMs selected from {cfg.assignments_file}")
        return 0

    if not cfg.golden_finalized_image.is_file() or not cfg.golden_finalized_vars.is_file():
        raise FileNotFoundError(
            "Missing finalized golden image/UEFI state. Run finalize-golden first:\n"
            f"  {cfg.golden_finalized_image}\n  {cfg.golden_finalized_vars}"
        )

    for row in rows:
        require_fields(row, ["vm", "course", "forgejo", "full_name", "email"], command="build-vms")
        host_file = REPO_ROOT / "hosts" / f"{row.vm}.nix"
        if not host_file.is_file():
            raise FileNotFoundError(
                f"Missing reviewed host file for {row.vm}: {host_file}. Run generate-hosts if the classroom mapping changed."
            )

    print(f"Build VMs from finalized golden ({cfg.mode})")
    print(f"  source: {cfg.golden_finalized_image}")
    for row in rows:
        _describe_row(cfg, row)

    if cfg.run.dry_run:
        print("Dry run: finished VM outputs would be skipped; missing ones would be rebuilt from the finalized golden.")
        return 0

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_log_dir = cfg.logs_dir / f"build-vms-{cfg.mode}-{timestamp}"
    run_log_dir.mkdir(parents=True, exist_ok=True)
    print(f"Logs: {run_log_dir}")

    cfg.vm_images_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mct-vm-lockdown-") as temp_name:
        lockdown_bundle: tuple[Path, str, str] | None = None
        if cfg.mode == "lockdown":
            lockdown_bundle = _prepare_lockdown_bundle(cfg, Path(temp_name))
            print(f"Lockdown repository bundle ready: {lockdown_bundle[1]} ({lockdown_bundle[2]})")

        built_count = 0
        skipped_count = 0
        for index, row in enumerate(rows, start=1):
            vm = row.vm
            course = row.raw["course"].strip()
            student = row.raw["forgejo"].strip()
            full_name = row.raw["full_name"].strip()
            email = row.raw["email"].strip()
            artifacts = image_artifacts(vm, cfg.vm_suffix)
            disk = artifacts.qcow2(cfg.vm_images_dir)
            vars_file = artifacts.vars(cfg.vm_images_dir)
            building_disk = artifacts.building_qcow2(cfg.vm_images_dir)
            building_vars = artifacts.building_vars(cfg.vm_images_dir)
            vm_log = run_log_dir / f"{vm}.log"
            qemu_log = run_log_dir / f"{vm}-qemu.log"

            state = _final_pair_state(disk, vars_file)
            if state == "complete":
                print(f"\n=== [{index}/{len(rows)}] {vm}: finished output exists — SKIP ===")
                skipped_count += 1
                continue
            if state == "partial":
                raise RuntimeError(
                    f"Inconsistent finished VM artifacts for {vm}:\n  {disk}\n  {vars_file}\n"
                    "Run reset-vms to remove the inconsistent output explicitly."
                )

            print(f"\n=== [{index}/{len(rows)}] {vm}: {full_name} / {course} / {student} ===")
            if building_disk.exists() or building_vars.exists():
                # A .building pair is never a finished result. Ensure it is not
                # still open by a diagnostic QEMU before discarding it.
                check_ssh_port_free()
                print(f"[{vm}] discarding stale .building artifacts from an interrupted run")
                building_disk.unlink(missing_ok=True)
                building_vars.unlink(missing_ok=True)

            print(f"[{vm}] copying finalized golden -> temporary VM")
            _copy_qcow2(cfg.golden_finalized_image, building_disk)
            _copy_plain(cfg.golden_finalized_vars, building_vars)
            _append_log(vm_log, f"VM={vm}\nmode={cfg.mode}\ncourse={course}\nstudent={student}\ndisk={building_disk}\n")

            with qemu_log.open("wb") as qlog:
                qemu = start_qemu(
                    disk=building_disk,
                    vars_file=building_vars,
                    headless=True,
                    discard=True,
                    stdout=qlog,
                )

            try:
                print(f"[{vm}] waiting for provisioning SSH...")
                wait_for_ssh_service(qemu=qemu)
                verify_ssh_login(cfg.preparation_host_key)

                _run_logged(
                    [*ssh_base(cfg.preparation_host_key), "test -f /home/student/NixOS-Bunny/flake.nix && test -d /home/student/NixOS-Bunny/hosts"],
                    log_path=vm_log,
                )
                local_host_file = REPO_ROOT / "hosts" / f"{vm}.nix"
                guest_host_file = f"/home/student/NixOS-Bunny/hosts/{vm}.nix"
                print(f"[{vm}] syncing reviewed hosts/{vm}.nix into the guest...")
                _run_logged(
                    [*ssh_base(cfg.preparation_host_key), f"cat > {shlex.quote(guest_host_file)}"],
                    log_path=vm_log,
                    input_text=local_host_file.read_text(encoding="utf-8"),
                    echo=False,
                )

                # Both modes first enter the normal host-specific generation.
                # This activates the correct identity and gives us an unrestricted
                # network for repository provisioning.
                print(f"[{vm}] nixos-rebuild -> .#{vm}")
                _run_logged(
                    [*ssh_base(cfg.preparation_host_key), f"sudo nixos-rebuild switch --flake path:/home/student/NixOS-Bunny#{shlex.quote(vm)}"],
                    log_path=vm_log,
                )
                print(f"[{vm}] rebooting into host-specific generation...")
                _append_log(vm_log, "$ sudo systemctl reboot")
                reboot_guest(key=cfg.preparation_host_key, qemu=qemu)

                hostname_proc = _run_logged(
                    [*ssh_base(cfg.preparation_host_key), "hostname"],
                    log_path=vm_log,
                    echo=False,
                )
                actual_hostname = (hostname_proc.stdout or "").strip()
                if actual_hostname != vm:
                    raise RuntimeError(f"hostname after reboot is {actual_hostname!r}, expected {vm!r}")

                if cfg.mode == "classroom":
                    repo = f"MCT_{course}"
                    github_url = _format_url(cfg.course_public_source, repo=repo, course=course)
                    forgejo_url = _format_url(cfg.course_student_origin, repo=repo, course=course)
                    print(f"[{vm}] provisioning {repo} without Forgejo credentials...")
                    _run_logged(
                        remote_script_command(
                            cfg.preparation_host_key,
                            [course, student, full_name, email, github_url, forgejo_url],
                        ),
                        log_path=vm_log,
                        input_text=_classroom_provision_script(),
                    )
                    print(f"[{vm}] validating classroom image...")
                    _run_logged(
                        remote_script_command(
                            cfg.preparation_host_key,
                            [vm, course, student, full_name, email, forgejo_url],
                        ),
                        log_path=vm_log,
                        input_text=_classroom_validate_script(),
                    )
                    if cfg.optimize_image_size:
                        _run_logged(
                            [*ssh_base(cfg.preparation_host_key), "sudo fstrim -av"],
                            log_path=vm_log,
                            check=False,
                        )
                    print(f"[{vm}] clean shutdown...")
                    poweroff_guest(key=cfg.preparation_host_key, qemu=qemu)

                else:
                    assert lockdown_bundle is not None
                    bundle, repo_name, repo_branch = lockdown_bundle
                    print(f"[{vm}] installing local exam repository {repo_name} ({repo_branch})...")
                    _copy_binary_via_ssh(
                        source=bundle,
                        remote_path="/tmp/mct-exam.bundle",
                        key=cfg.preparation_host_key,
                        log_path=vm_log,
                    )
                    exam_remote = (
                        f"https://{cfg.forgejo_host}/{cfg.forgejo_exam_owner}/"
                        f"{repo_name}_{student}.git"
                    )
                    _run_logged(
                        remote_script_command(
                            cfg.preparation_host_key,
                            [repo_name, repo_branch, full_name, email, course, student, exam_remote],
                        ),
                        log_path=vm_log,
                        input_text=_lockdown_repo_script(),
                    )

                    # The lockdown switch must be the last SSH session. Once the
                    # firewall is active, new provisioning SSH connections are
                    # intentionally blocked. Cleanup, validation and poweroff all
                    # happen inside this already-established connection.
                    target = f"{vm}-lockdown"
                    print(f"[{vm}] switching to final lockdown generation and removing older generations...")
                    proc = _run_logged(
                        remote_script_command(cfg.preparation_host_key, [target, target]),
                        log_path=vm_log,
                        input_text=_lockdown_finalize_script(),
                        check=False,
                    )
                    if "LOCKDOWN_FINALIZATION_OK" not in (proc.stdout or ""):
                        raise RuntimeError(
                            "Lockdown finalization did not reach its success marker; see VM log"
                        )
                    try:
                        qemu.wait(timeout=90)
                    except subprocess.TimeoutExpired:
                        stop_qemu(qemu)
                        raise TimeoutError("Lockdown VM did not power off after finalization")
                    if qemu.returncode not in (0, None):
                        # QEMU normally exits 0 on guest poweroff. Treat another
                        # status as suspicious even if the guest printed success.
                        raise RuntimeError(f"QEMU exited with status {qemu.returncode} after lockdown poweroff")

                building_disk.replace(disk)
                building_vars.replace(vars_file)
                built_count += 1
                print(f"[{vm}] DONE -> {disk.name}")

            except Exception:
                print(f"[{vm}] FAILED — see {vm_log} and {qemu_log}", file=sys.stderr)
                if cfg.run.keep_failed_vm_running:
                    print(f"[{vm}] QEMU left running for inspection on 127.0.0.1:2222.", file=sys.stderr)
                else:
                    try:
                        subprocess.run([*ssh_base(cfg.preparation_host_key), "sudo systemctl poweroff"], check=False)
                        qemu.wait(timeout=20)
                    except Exception:
                        stop_qemu(qemu)
                raise

        print(f"\nBuild complete: {built_count} built, {skipped_count} already complete.")
        return 0
