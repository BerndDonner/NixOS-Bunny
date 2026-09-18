from __future__ import annotations

import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import AppConfig, REPO_ROOT
from .csv_model import CsvRow, read_rollout_csv, require_fields
from .runtime import (
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
    active = doc.active_rows()
    if not cfg.run.only_vms:
        return active
    active_by_vm = {row.vm: row for row in active}
    missing = sorted(cfg.run.only_vms - set(active_by_vm))
    if missing:
        raise ValueError(
            "[run].only_vms contains VM(s) that are not active in "
            f"{cfg.assignments_file}: {', '.join(missing)}"
        )
    return [row for row in active if row.vm in cfg.run.only_vms]


def _format_url(template: str, *, repo: str, course: str) -> str:
    try:
        return template.format(repo=repo, course=course)
    except KeyError as exc:
        raise ValueError(
            f"Unknown placeholder in URL template {template!r}: {exc}. "
            "Supported placeholders are {repo} and {course}."
        ) from exc


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


def _provision_script() -> str:
    return r'''set -euo pipefail

course=$1
student=$2
full_name=$3
email=$4
github_url=$5
forgejo_url=$6

repo="MCT_${course}"
repo_dir="$HOME/$repo"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

expect_config() {
    key=$1
    expected=$2
    actual=$(git config --global --get "$key" 2>/dev/null || true)
    if [[ "$actual" != "$expected" ]]; then
        fail "$key: expected '$expected', got '${actual:-<unset>}'"
    fi
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
            cloned=1
            break
        fi
        echo "Clone attempt $attempt/10 failed; retrying in 2 seconds..." >&2
        rm -rf "$repo_dir"
        sleep 2
    done
    [[ "$cloned" -eq 1 ]] || fail "could not clone $github_url"
fi

cd "$repo_dir"

# GitHub is only a bootstrap source. Ensure origin points at Forgejo, then
# remove the bootstrap remote and any tracking metadata that clone may have
# attached to master. The final VM must expose Forgejo only.
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

# Guard against provisioning from an old course repository. Continue belongs to
# phase 2 and arduino-cli.yaml was intentionally removed from the template.
if grep -Eq '\.continue/config\.yaml|arduino-cli\.yaml' _config/setup.sh; then
    fail "course repository contains an obsolete _config/setup.sh; publish the fixed MCT template first"
fi
if [[ -e arduino-cli.yaml ]]; then
    fail "obsolete arduino-cli.yaml is still present in the course repository"
fi

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

    # The branch is intentionally unpublished at image-build time. The student
    # creates origin/<login> later with the first `git pub`.
    git config --local --unset-all "branch.$student.remote" 2>/dev/null || true
    git config --local --unset-all "branch.$student.merge" 2>/dev/null || true
fi

bash _config/setup.sh
'''


def _validate_script() -> str:
    return r'''set -euo pipefail

vm=$1
course=$2
student=$3
full_name=$4
email=$5
forgejo_url=$6

repo="MCT_${course}"
repo_dir="$HOME/$repo"

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

[[ "$(hostname)" == "$vm" ]] || fail "hostname is $(hostname), expected $vm"
[[ "$(git config --global --get user.name 2>/dev/null || true)" == "$full_name" ]] || fail "wrong Git user.name"
[[ "$(git config --global --get user.email 2>/dev/null || true)" == "$email" ]] || fail "wrong Git user.email"
[[ "$(git config --global --get mct.student 2>/dev/null || true)" == "$student" ]] || fail "wrong mct.student"
[[ "$(git config --global --get mct.course 2>/dev/null || true)" == "$course" ]] || fail "wrong mct.course"

[[ -d "$HOME/reference" ]] || fail "offline documentation directory $HOME/reference is missing"
command -v code >/dev/null || fail "VS Code command 'code' is missing"
command -v google-chrome >/dev/null || command -v google-chrome-stable >/dev/null || fail "Google Chrome command is missing"
[[ -f "$HOME/.continue/config.yaml" ]] || fail "final Continue configuration is missing; finalize-golden was not completed"

[[ -d "$repo_dir/.git" ]] || fail "$repo_dir is not a Git repository"
cd "$repo_dir"

expected_branch=$student
if [[ "$student" == "donner" ]]; then
    expected_branch=master
fi
[[ "$(git branch --show-current)" == "$expected_branch" ]] || fail "wrong Git branch"
if git remote get-url github >/dev/null 2>&1; then
    fail "GitHub bootstrap remote is still present"
fi
[[ "$(git remote)" == "origin" ]] || fail "finished repository must contain only the Forgejo origin remote"
[[ "$(git remote get-url origin)" == "$forgejo_url" ]] || fail "wrong origin remote URL"
# No branch in any finished VM may still track the removed GitHub remote.
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


def _describe_row(cfg: AppConfig, row: CsvRow) -> None:
    course = row.raw["course"].strip()
    student = row.raw["forgejo"].strip()
    repo = f"MCT_{course}"
    github_url = _format_url(cfg.course_public_source, repo=repo, course=course)
    forgejo_url = _format_url(cfg.course_student_origin, repo=repo, course=course)
    stem = f"{row.vm}{cfg.vm_suffix}"
    print(f"{row.vm}:")
    print(f"  image     : {cfg.vm_images_dir / (stem + '.qcow2')}")
    print(f"  identity  : {row.raw['full_name']} <{row.raw['email']}> / {student}")
    print(f"  course    : {course} -> {repo}")
    print(f"  source    : {github_url}")
    print(f"  origin    : {forgejo_url} (configured only; no login/push)")
    print(f"  branch    : {'master' if student == 'donner' else student}")


def individualize_images(cfg: AppConfig) -> int:
    if cfg.mode != "classroom":
        raise RuntimeError(
            "individualize currently implements the classroom workflow only. "
            "Lockdown individualization is intentionally left untouched until the exam workflow is reviewed."
        )

    rows = _selected_rows(cfg)
    if not rows:
        print(f"WARN: no active VM rows found in {cfg.assignments_file}")
        return 0

    for row in rows:
        require_fields(row, ["vm", "course", "forgejo", "full_name", "email"], command="individualize")
        host_file = REPO_ROOT / "hosts" / f"{row.vm}.nix"
        if not host_file.is_file():
            raise FileNotFoundError(
                f"Missing reviewed host file for {row.vm}: {host_file}. "
                "Run generate-nix only if the rollout mapping actually changed."
            )

    print("Phase 3 — individualize existing QCOW2 images")
    for row in rows:
        _describe_row(cfg, row)

    if cfg.run.dry_run:
        print("Dry run: no VM will be started or modified.")
        return 0

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_log_dir = cfg.logs_dir / f"individualize-{timestamp}"
    run_log_dir.mkdir(parents=True, exist_ok=True)
    print(f"Logs: {run_log_dir}")

    for index, row in enumerate(rows, start=1):
        vm = row.vm
        course = row.raw["course"].strip()
        student = row.raw["forgejo"].strip()
        full_name = row.raw["full_name"].strip()
        email = row.raw["email"].strip()
        repo = f"MCT_{course}"
        github_url = _format_url(cfg.course_public_source, repo=repo, course=course)
        forgejo_url = _format_url(cfg.course_student_origin, repo=repo, course=course)

        stem = f"{vm}{cfg.vm_suffix}"
        disk = cfg.vm_images_dir / f"{stem}.qcow2"
        vars_file = cfg.vm_images_dir / f"{stem}.OVMF_VARS.fd"
        vm_log = run_log_dir / f"{vm}.log"
        qemu_log = run_log_dir / f"{vm}-qemu.log"

        if not disk.is_file():
            raise FileNotFoundError(f"Missing QCOW2 for {vm}: {disk}")
        if not vars_file.is_file():
            raise FileNotFoundError(f"Missing OVMF VARS for {vm}: {vars_file}")

        print(f"\n=== [{index}/{len(rows)}] {vm}: {full_name} / {course} / {student} ===")
        _append_log(vm_log, f"VM={vm}\ncourse={course}\nstudent={student}\ndisk={disk}\n")

        with qemu_log.open("wb") as qlog:
            qemu = start_qemu(
                disk=disk,
                vars_file=vars_file,
                headless=True,
                discard=True,
                stdout=qlog,
            )

        try:
            print(f"[{vm}] waiting for provisioning SSH...")
            wait_for_ssh_service(qemu=qemu)
            verify_ssh_login(cfg.preparation_host_key)

            guest_host_file = f"/home/student/NixOS-Bunny/hosts/{vm}.nix"
            _run_logged(
                [*ssh_base(cfg.preparation_host_key), "test -f /home/student/NixOS-Bunny/flake.nix && test -d /home/student/NixOS-Bunny/hosts"],
                log_path=vm_log,
            )

            local_host_file = REPO_ROOT / "hosts" / f"{vm}.nix"
            print(f"[{vm}] syncing reviewed hosts/{vm}.nix into the guest...")
            _run_logged(
                [*ssh_base(cfg.preparation_host_key), f"cat > {shlex.quote(guest_host_file)}"],
                log_path=vm_log,
                input_text=local_host_file.read_text(encoding="utf-8"),
                echo=False,
            )

            print(f"[{vm}] nixos-rebuild -> .#{vm}")
            _run_logged(
                [*ssh_base(cfg.preparation_host_key), f"sudo nixos-rebuild switch --flake path:/home/student/NixOS-Bunny#{shlex.quote(vm)}"],
                log_path=vm_log,
            )

            # A switch writes the new /etc/hostname, but the running kernel may
            # still report the generic golden-image hostname ("bunny"). Reboot
            # before any course provisioning so we both activate and test the
            # host-specific system generation.
            print(f"[{vm}] rebooting into the individualized NixOS generation...")
            _append_log(vm_log, "$ sudo systemctl reboot")
            reboot_guest(key=cfg.preparation_host_key, qemu=qemu)

            hostname_proc = _run_logged(
                [*ssh_base(cfg.preparation_host_key), "hostname"],
                log_path=vm_log,
                echo=False,
            )
            actual_hostname = (hostname_proc.stdout or "").strip()
            if actual_hostname != vm:
                raise RuntimeError(
                    f"hostname after reboot is {actual_hostname!r}, expected {vm!r}"
                )
            print(f"[{vm}] reboot complete; hostname is {actual_hostname}")

            print(f"[{vm}] provisioning {repo} without Forgejo credentials...")
            _run_logged(
                remote_script_command(cfg.preparation_host_key, [course, student, full_name, email, github_url, forgejo_url]),
                log_path=vm_log,
                input_text=_provision_script(),
            )

            print(f"[{vm}] validating image...")
            _run_logged(
                remote_script_command(cfg.preparation_host_key, [vm, course, student, full_name, email, forgejo_url]),
                log_path=vm_log,
                input_text=_validate_script(),
            )

            if cfg.optimize_image_size:
                print(f"[{vm}] optimizing image size...")
                _run_logged([*ssh_base(cfg.preparation_host_key), "sudo fstrim -av"], log_path=vm_log, check=False)

            print(f"[{vm}] clean shutdown...")
            poweroff_guest(key=cfg.preparation_host_key, qemu=qemu)
            print(f"[{vm}] DONE")

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

    print(f"\nAll {len(rows)} selected VM(s) individualized successfully.")
    return 0
