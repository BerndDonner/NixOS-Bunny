from __future__ import annotations

import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .csv_model import CsvRow, read_rollout_csv, require_fields


DEFAULT_GITHUB_URL_TEMPLATE = "https://github.com/BerndDonner/{repo}.git"
DEFAULT_FORGEJO_URL_TEMPLATE = "https://forgejo.meisterk.de/Microcontrollertechnik/{repo}.git"
DEFAULT_SSH_PORT = 2222
DEFAULT_SSH_TIMEOUT = 240
DEFAULT_SHUTDOWN_TIMEOUT = 120


@dataclass(frozen=True)
class IndividualizeOptions:
    csv_path: str
    image_dir: str
    vm_suffix: str = ""
    only_vms: frozenset[str] = frozenset()
    ssh_port: int = DEFAULT_SSH_PORT
    ssh_key: str | None = None
    ssh_timeout: int = DEFAULT_SSH_TIMEOUT
    shutdown_timeout: int = DEFAULT_SHUTDOWN_TIMEOUT
    qemu_script: str | None = None
    logs_dir: str = "logs"
    github_url_template: str = DEFAULT_GITHUB_URL_TEMPLATE
    forgejo_url_template: str = DEFAULT_FORGEJO_URL_TEMPLATE
    chrome_start_page: str = "auto"
    vscode_autostart: bool = True
    trim: bool = True
    dry_run: bool = False
    keep_on_error: bool = False


def _need_cmd(name: str) -> None:
    if shutil.which(name) is None:
        raise FileNotFoundError(f"Missing required command in PATH: {name}")


def _selected_rows(csv_path: str, only_vms: frozenset[str]) -> list[CsvRow]:
    doc = read_rollout_csv(csv_path)
    active = doc.active_rows()

    if not only_vms:
        return active

    active_by_vm = {row.vm: row for row in active}
    missing = sorted(only_vms - set(active_by_vm))
    if missing:
        raise ValueError(
            "--only contains VM(s) that are not active in "
            f"{csv_path}: {', '.join(missing)}"
        )

    return [row for row in active if row.vm in only_vms]


def _format_url(template: str, *, repo: str, course: str) -> str:
    try:
        return template.format(repo=repo, course=course)
    except KeyError as exc:
        raise ValueError(
            f"Unknown placeholder in URL template {template!r}: {exc}. "
            "Supported placeholders are {repo} and {course}."
        ) from exc


def _qemu_script_path(value: str | None) -> Path:
    if value:
        path = Path(value).expanduser().resolve()
    else:
        path = Path(__file__).resolve().parents[1] / "run-qemu.sh"

    if not path.is_file():
        raise FileNotFoundError(f"QEMU helper not found: {path}")
    if not path.stat().st_mode & 0o111:
        raise PermissionError(f"QEMU helper is not executable: {path}")
    return path


def _ssh_base(*, port: int, key: str | None) -> list[str]:
    cmd = [
        "ssh",
        "-p",
        str(port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
    ]

    if key:
        key_path = Path(key).expanduser().resolve()
        if not key_path.is_file():
            raise FileNotFoundError(f"SSH private key not found: {key_path}")
        cmd.extend(["-o", "IdentitiesOnly=yes", "-i", str(key_path)])

    cmd.append("student@127.0.0.1")
    return cmd


def _check_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"TCP port 127.0.0.1:{port} is already in use. "
                "Stop the old QEMU/SSH forward or choose --ssh-port."
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


def _wait_for_ssh(
    *,
    qemu: subprocess.Popen[bytes],
    ssh_base: list[str],
    timeout: int,
    log_path: Path,
) -> None:
    deadline = time.monotonic() + timeout
    last_output = ""

    while time.monotonic() < deadline:
        qemu_rc = qemu.poll()
        if qemu_rc is not None:
            raise RuntimeError(
                f"QEMU exited before SSH became available (exit {qemu_rc}). "
                f"See {log_path}."
            )

        probe = subprocess.run(
            [*ssh_base, "true"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        last_output = (probe.stdout or "").strip()
        if probe.returncode == 0:
            return

        if "Permission denied" in last_output:
            raise RuntimeError(
                "SSH authentication failed for the provisioning key. "
                "Load the private key into ssh-agent once (ssh-add ...) or use --ssh-key."
            )

        time.sleep(2)

    detail = f" Last SSH output: {last_output}" if last_output else ""
    raise TimeoutError(f"SSH did not become available within {timeout}s.{detail}")


def _remote_script_command(ssh_base: list[str], args: list[str]) -> list[str]:
    quoted = " ".join(shlex.quote(arg) for arg in args)
    return [*ssh_base, f"bash -s -- {quoted}"]


def _provision_script() -> str:
    return r'''set -euo pipefail

course=$1
student=$2
full_name=$3
email=$4
github_url=$5
forgejo_url=$6
chrome_start=$7
vscode_autostart=$8

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

if git remote get-url github >/dev/null 2>&1; then
    git remote set-url github "$github_url"
else
    git remote add github "$github_url"
fi

if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$forgejo_url"
else
    git remote add origin "$forgejo_url"
fi

# Guard against provisioning from an old course repository.  The course setup
# must not copy Continue config and arduino-cli.yaml is intentionally gone.
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

    # The branch is intentionally unpublished at image-build time.  The
    # student creates origin/<login> later with the first `git pub`.
    git config --local --unset-all "branch.$student.remote" 2>/dev/null || true
    git config --local --unset-all "branch.$student.merge" 2>/dev/null || true
fi

bash _config/setup.sh

# Make the course the obvious first VS Code workspace.  Git has already selected
# the correct local branch, so opening the folder also opens the right branch.
if [[ "$vscode_autostart" == "1" ]]; then
    mkdir -p "$HOME/.config/autostart"
    cat > "$HOME/.config/autostart/mct-vscode.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MCT VS Code
Exec=code $repo_dir
OnlyShowIn=KDE;
X-KDE-autostart-after=panel
X-KDE-StartupNotify=false
Terminal=false
EOF
else
    rm -f "$HOME/.config/autostart/mct-vscode.desktop"
fi

resolve_chrome_start() {
    local requested=$1
    local candidate=""

    if [[ "$requested" == "none" ]]; then
        return 1
    fi

    if [[ "$requested" == "auto" ]]; then
        for candidate in \
            "$HOME/reference/index.html" \
            "$HOME/reference/index.htm"
        do
            if [[ -f "$candidate" ]]; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done

        if [[ -d "$HOME/reference" ]]; then
            candidate=$(find "$HOME/reference" -type f \
                \( -iname 'index.html' -o -iname 'index.htm' \) \
                -print | LC_ALL=C sort | head -n 1 || true)
            if [[ -n "$candidate" ]]; then
                printf '%s\n' "$candidate"
                return 0
            fi

            # No HTML entry point: Chrome can still open the local directory
            # and provide a file listing instead of making phase 3 fail.
            printf '%s/\n' "$HOME/reference"
            return 0
        fi
        return 2
    fi

    case "$requested" in
        *://*)
            printf '%s\n' "$requested"
            return 0
            ;;
        "~/"*)
            candidate="$HOME/${requested#~/}"
            ;;
        /*)
            candidate="$requested"
            ;;
        *)
            candidate="$HOME/$requested"
            ;;
    esac

    [[ -f "$candidate" ]] || fail "Chrome start page does not exist: $candidate"
    printf '%s\n' "$candidate"
}

chrome_value=""
set +e
chrome_value=$(resolve_chrome_start "$chrome_start")
chrome_rc=$?
set -e

if [[ "$chrome_rc" -eq 2 ]]; then
    fail "could not auto-detect an offline documentation index below $HOME/reference; use --chrome-start-page <path-or-url>"
elif [[ "$chrome_rc" -eq 0 ]]; then
    if [[ "$chrome_value" == *://* ]]; then
        chrome_url="$chrome_value"
    else
        chrome_url="file://$chrome_value"
    fi

    policy_tmp=$(mktemp)
    jq -n --arg url "$chrome_url" '{
        HomepageLocation: $url,
        HomepageIsNewTabPage: false,
        ShowHomeButton: true,
        RestoreOnStartup: 4,
        RestoreOnStartupURLs: [$url]
    }' > "$policy_tmp"

    sudo install -Dm0644 "$policy_tmp" /etc/opt/chrome/policies/managed/mct-classroom.json
    rm -f "$policy_tmp"
    echo "Chrome start page: $chrome_url"
else
    sudo rm -f /etc/opt/chrome/policies/managed/mct-classroom.json
    echo "Chrome start-page configuration skipped; classroom policy removed."
fi
'''


def _validate_script() -> str:
    return r'''set -euo pipefail

vm=$1
course=$2
student=$3
full_name=$4
email=$5
github_url=$6
forgejo_url=$7
chrome_start=$8
vscode_autostart=$9

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

[[ -d "$repo_dir/.git" ]] || fail "$repo_dir is not a Git repository"
cd "$repo_dir"

expected_branch=$student
if [[ "$student" == "donner" ]]; then
    expected_branch=master
fi
[[ "$(git branch --show-current)" == "$expected_branch" ]] || fail "wrong Git branch"
[[ "$(git remote get-url github)" == "$github_url" ]] || fail "wrong github remote URL"
[[ "$(git remote get-url origin)" == "$forgejo_url" ]] || fail "wrong origin remote URL"
[[ "$(git config --local --get core.hooksPath 2>/dev/null || true)" == "_config/hooks" ]] || fail "course hooks are not active"
git config --local --get-all include.path | grep -Fxq '../_config/gitconfig' || fail "course gitconfig include is missing"
[[ -f .vscode/settings.json ]] || fail ".vscode/settings.json is missing"
[[ -f .vscode/launch.json ]] || fail ".vscode/launch.json is missing"

if [[ "$student" == "donner" ]]; then
    [[ "$(git config --local --get branch.master.remote 2>/dev/null || true)" == "origin" ]] || fail "teacher master remote metadata is wrong"
    [[ "$(git config --local --get branch.master.merge 2>/dev/null || true)" == "refs/heads/master" ]] || fail "teacher master merge metadata is wrong"
else
    if git config --local --get "branch.$student.remote" >/dev/null 2>&1; then
        fail "student branch already has an upstream; first publication must remain `git pub`"
    fi
    grep -Fq '"files.readonlyInclude"' .vscode/settings.json || fail "VS Code read-only protection is missing"
    grep -Fq "$student" .vscode/settings.json || fail "VS Code settings do not contain the student login"
fi

if [[ "$vscode_autostart" == "1" ]]; then
    [[ -f "$HOME/.config/autostart/mct-vscode.desktop" ]] || fail "VS Code autostart file is missing"
    grep -Fq "Exec=code $repo_dir" "$HOME/.config/autostart/mct-vscode.desktop" || fail "VS Code autostart points to the wrong repository"
fi

if [[ "$chrome_start" != "none" ]]; then
    [[ -f /etc/opt/chrome/policies/managed/mct-classroom.json ]] || fail "Chrome classroom policy is missing"
    jq -e '.HomepageLocation and (.RestoreOnStartup == 4) and (.RestoreOnStartupURLs | length == 1)' \
        /etc/opt/chrome/policies/managed/mct-classroom.json >/dev/null || fail "Chrome classroom policy is invalid"
fi

# setup.sh may create ignored per-user files, but the versioned course tree must
# remain clean after provisioning.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    git status --short
    fail "versioned course repository files changed during provisioning"
fi

# Continue is deliberately not managed by phase 3.  Do not validate, create,
# overwrite or delete ~/.continue/config.yaml here.

echo "Validation OK: $vm / $course / $expected_branch"
'''


def _poweroff(
    *,
    ssh_base: list[str],
    qemu: subprocess.Popen[bytes],
    timeout: int,
    log_path: Path,
) -> None:
    # SSH normally disconnects while systemd powers the guest off, so a nonzero
    # return code here is not by itself an error.
    _run_logged(
        [*ssh_base, "sudo systemctl poweroff"],
        log_path=log_path,
        check=False,
        echo=False,
    )

    try:
        qemu.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        qemu.terminate()
        try:
            qemu.wait(timeout=10)
        except subprocess.TimeoutExpired:
            qemu.kill()
            qemu.wait(timeout=10)
        raise TimeoutError(
            f"QEMU did not exit within {timeout}s after guest poweroff; process was terminated"
        )


def _stop_qemu(qemu: subprocess.Popen[bytes]) -> None:
    if qemu.poll() is not None:
        return
    qemu.terminate()
    try:
        qemu.wait(timeout=10)
    except subprocess.TimeoutExpired:
        qemu.kill()
        qemu.wait(timeout=10)


def _describe_row(row: CsvRow, *, image_root: Path, vm_suffix: str, options: IndividualizeOptions) -> None:
    course = row.raw["course"].strip()
    student = row.raw["forgejo"].strip()
    repo = f"MCT_{course}"
    github_url = _format_url(options.github_url_template, repo=repo, course=course)
    forgejo_url = _format_url(options.forgejo_url_template, repo=repo, course=course)
    stem = f"{row.vm}{vm_suffix}"
    print(f"{row.vm}:")
    print(f"  image     : {image_root / (stem + '.qcow2')}")
    print(f"  identity  : {row.raw['full_name']} <{row.raw['email']}> / {student}")
    print(f"  course    : {course} -> {repo}")
    print(f"  github    : {github_url}")
    print(f"  forgejo   : {forgejo_url} (configured only; no login/push)")
    print(f"  branch    : {'master' if student == 'donner' else student}")
    print(f"  Chrome    : {options.chrome_start_page}")
    print(f"  VS Code   : {'autostart course folder' if options.vscode_autostart else 'no autostart'}")


def individualize_images(options: IndividualizeOptions) -> int:
    rows = _selected_rows(options.csv_path, options.only_vms)
    if not rows:
        print(f"WARN: no active VM rows found in {options.csv_path}")
        return 0

    image_root = Path(options.image_dir).expanduser().resolve()
    qemu_script = _qemu_script_path(options.qemu_script)
    config_root = Path(__file__).resolve().parents[2]

    for row in rows:
        require_fields(
            row,
            ["vm", "course", "forgejo", "full_name", "email"],
            command="individualize",
        )
        local_host_file = config_root / "hosts" / f"{row.vm}.nix"
        if not local_host_file.is_file():
            raise FileNotFoundError(
                f"Missing reviewed host file for {row.vm}: {local_host_file}. "
                "Run generate-nix only if the rollout mapping actually changed."
            )

    if options.dry_run:
        print("Phase-3 individualization dry run; no VM will be started or modified.\n")
        for row in rows:
            _describe_row(row, image_root=image_root, vm_suffix=options.vm_suffix, options=options)
        return 0

    _need_cmd("ssh")
    _check_port_free(options.ssh_port)
    ssh_base = _ssh_base(port=options.ssh_port, key=options.ssh_key)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_log_dir = Path(options.logs_dir).expanduser().resolve() / f"individualize-{timestamp}"
    run_log_dir.mkdir(parents=True, exist_ok=True)
    print(f"Logs: {run_log_dir}")

    for index, row in enumerate(rows, start=1):
        vm = row.vm
        course = row.raw["course"].strip()
        student = row.raw["forgejo"].strip()
        full_name = row.raw["full_name"].strip()
        email = row.raw["email"].strip()
        repo = f"MCT_{course}"
        github_url = _format_url(options.github_url_template, repo=repo, course=course)
        forgejo_url = _format_url(options.forgejo_url_template, repo=repo, course=course)

        stem = f"{vm}{options.vm_suffix}"
        disk = image_root / f"{stem}.qcow2"
        vars_file = image_root / f"{stem}.OVMF_VARS.fd"
        vm_log = run_log_dir / f"{vm}.log"
        qemu_log = run_log_dir / f"{vm}-qemu.log"

        if not disk.is_file():
            raise FileNotFoundError(f"Missing QCOW2 for {vm}: {disk}")
        if not vars_file.is_file():
            raise FileNotFoundError(f"Missing OVMF VARS for {vm}: {vars_file}")

        print(f"\n=== [{index}/{len(rows)}] {vm}: {full_name} / {course} / {student} ===")
        _append_log(vm_log, f"VM={vm}\ncourse={course}\nstudent={student}\ndisk={disk}\n")

        qemu_cmd = [
            str(qemu_script),
            "--headless",
            "--discard",
            "--ssh",
            "--port",
            str(options.ssh_port),
            "--vars",
            str(vars_file),
            str(disk),
        ]

        with qemu_log.open("wb") as qlog:
            qemu = subprocess.Popen(qemu_cmd, stdout=qlog, stderr=subprocess.STDOUT)

        try:
            print(f"[{vm}] waiting for provisioning SSH...")
            _wait_for_ssh(
                qemu=qemu,
                ssh_base=ssh_base,
                timeout=options.ssh_timeout,
                log_path=qemu_log,
            )

            host_file = f"/home/student/NixOS-Bunny/hosts/{vm}.nix"
            preflight = "test -f /home/student/NixOS-Bunny/flake.nix && test -d /home/student/NixOS-Bunny/hosts"
            _run_logged([*ssh_base, preflight], log_path=vm_log)

            # Phase 3 uses the already reviewed/generated host file from the
            # host checkout.  Do not regenerate it and do not depend on the
            # golden image having cloned exactly the same GitHub revision.
            local_host_file = config_root / "hosts" / f"{vm}.nix"
            print(f"[{vm}] syncing reviewed hosts/{vm}.nix into the guest...")
            _run_logged(
                [*ssh_base, f"cat > {shlex.quote(host_file)}"],
                log_path=vm_log,
                input_text=local_host_file.read_text(encoding="utf-8"),
                echo=False,
            )

            print(f"[{vm}] nixos-rebuild -> .#{vm}")
            _run_logged(
                [
                    *ssh_base,
                    f"sudo nixos-rebuild switch --flake path:/home/student/NixOS-Bunny#{shlex.quote(vm)}",
                ],
                log_path=vm_log,
            )

            # A rebuild may restart sockets/services. Re-probe before provisioning.
            _wait_for_ssh(
                qemu=qemu,
                ssh_base=ssh_base,
                timeout=options.ssh_timeout,
                log_path=qemu_log,
            )

            print(f"[{vm}] provisioning {repo} without Forgejo credentials...")
            provision_args = [
                course,
                student,
                full_name,
                email,
                github_url,
                forgejo_url,
                options.chrome_start_page,
                "1" if options.vscode_autostart else "0",
            ]
            _run_logged(
                _remote_script_command(ssh_base, provision_args),
                log_path=vm_log,
                input_text=_provision_script(),
            )

            print(f"[{vm}] validating image...")
            validate_args = [
                vm,
                course,
                student,
                full_name,
                email,
                github_url,
                forgejo_url,
                options.chrome_start_page,
                "1" if options.vscode_autostart else "0",
            ]
            _run_logged(
                _remote_script_command(ssh_base, validate_args),
                log_path=vm_log,
                input_text=_validate_script(),
            )

            if options.trim:
                print(f"[{vm}] fstrim...")
                _run_logged(
                    [*ssh_base, "sudo fstrim -av"],
                    log_path=vm_log,
                    check=False,
                )

            print(f"[{vm}] clean shutdown...")
            _poweroff(
                ssh_base=ssh_base,
                qemu=qemu,
                timeout=options.shutdown_timeout,
                log_path=vm_log,
            )
            print(f"[{vm}] DONE")

        except Exception:
            print(f"[{vm}] FAILED — see {vm_log} and {qemu_log}", file=sys.stderr)
            if options.keep_on_error:
                print(
                    f"[{vm}] QEMU left running for inspection on SSH port {options.ssh_port}.",
                    file=sys.stderr,
                )
            else:
                # First try a clean guest shutdown if SSH is still usable; then
                # make sure the QEMU process cannot block the next run.
                try:
                    _run_logged(
                        [*ssh_base, "sudo systemctl poweroff"],
                        log_path=vm_log,
                        check=False,
                        echo=False,
                    )
                    qemu.wait(timeout=20)
                except Exception:
                    _stop_qemu(qemu)
            raise

    print(f"\nAll {len(rows)} selected VM(s) individualized successfully.")
    return 0
