#!/usr/bin/env python3
"""Forgejo administration for MCT lockdown/exam repositories.

The exam designation is the directory name configured as ``[lockdown].repo``.
For every active student row in ``scripts/config/rollout-lockdown.csv`` this
script can create one private, initially empty Forgejo repository:

    <exam>_<forgejo-login>

The local exam Git repository remains the authoritative starting state.  It is
copied into each lockdown VM by the existing git-bundle workflow; students push
that history to their personal Forgejo repository later over HTTPS.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from mct_vm.config import CONFIG_DIR, REPO_ROOT, load_config
from mct_vm.csv_model import CsvRow, read_rollout_csv, require_fields

LOCKDOWN_CSV = CONFIG_DIR / "rollout-lockdown.csv"


class ForgejoError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


class ForgejoClient:
    def __init__(self, base_url: str, token: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        data = None
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/json",
            "User-Agent": "nixos-bunny-forgejo-exam/1.0",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else None
                return response.status, payload
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
                message = payload.get("message") or raw or exc.reason
            except json.JSONDecodeError:
                message = raw or str(exc.reason)
            raise ForgejoError(exc.code, str(message)) from exc
        except URLError as exc:
            raise ForgejoError(None, f"Network error: {exc.reason}") from exc

    def current_user(self) -> dict[str, Any]:
        status, payload = self._request("GET", "/api/v1/user")
        if status != 200 or not isinstance(payload, dict):
            raise ForgejoError(status, "Unexpected response from /api/v1/user")
        return payload

    def get_repo(self, owner: str, name: str) -> dict[str, Any] | None:
        path = f"/api/v1/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        try:
            status, payload = self._request("GET", path)
            if status == 200 and isinstance(payload, dict):
                return payload
            raise ForgejoError(status, f"Unexpected response while reading {owner}/{name}")
        except ForgejoError as exc:
            if exc.status == 404:
                return None
            raise

    def add_collaborator(self, owner: str, repo: str, username: str) -> None:
        path = (
            f"/api/v1/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/collaborators/{quote(username, safe='')}"
        )
        status, _ = self._request("PUT", path, {"permission": "write"})
        if status not in {201, 204}:
            raise ForgejoError(status, f"Unexpected response while granting {username} access to {owner}/{repo}")

    def remove_collaborator(self, owner: str, repo: str, username: str) -> bool:
        path = (
            f"/api/v1/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/collaborators/{quote(username, safe='')}"
        )
        try:
            status, _ = self._request("DELETE", path)
        except ForgejoError as exc:
            if exc.status == 404:
                return False
            raise
        if status != 204:
            raise ForgejoError(status, f"Unexpected response while revoking {username} access to {owner}/{repo}")
        return True

    def create_repo(self, *, name: str, description: str) -> dict[str, Any]:
        body = {
            "name": name,
            "description": description,
            "private": True,
            "auto_init": False,
            "has_issues": False,
            "has_projects": False,
            "has_wiki": False,
        }
        status, payload = self._request("POST", "/api/v1/user/repos", body)
        if status != 201 or not isinstance(payload, dict):
            raise ForgejoError(status, f"Unexpected response while creating repository {name}")
        return payload


def _running_native_windows_python_in_git_bash() -> bool:
    return os.name == "nt" and bool(os.environ.get("MSYSTEM"))


def prompt_secret(prompt: str) -> str:
    if _running_native_windows_python_in_git_bash():
        shell_code = (
            'IFS= read -r -s -p "$1" secret </dev/tty; '
            'status=$?; printf "\\n" >/dev/tty; '
            '[ "$status" -eq 0 ] || exit "$status"; printf "%s" "$secret"'
        )
        try:
            result = subprocess.run(
                ["bash", "-c", shell_code, "_", prompt],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            return result.stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "Secure token input failed in Git Bash; set FORGEJO_TOKEN as a fallback."
            ) from exc
    return getpass.getpass(prompt).strip()


def get_token() -> str:
    token = os.environ.get("FORGEJO_TOKEN", "").strip()
    if token:
        return token
    return prompt_secret("Forgejo API token: ")


def _exam_repo() -> Path:
    cfg = load_config()
    repo = cfg.lockdown_repo
    if repo is None:
        raise ValueError("[lockdown].repo is empty in scripts/config/config.toml")

    repos_dir = (REPO_ROOT / "repos").resolve()
    repo = repo.resolve()
    if repo.parent != repos_dir:
        raise ValueError(
            "[lockdown].repo must be a direct child of repos/ so its directory "
            "name can be used as the exam designation"
        )
    if not (repo / ".git").exists():
        raise FileNotFoundError(f"Lockdown repository is not a Git repository: {repo}")
    return repo


def _student_rows(owner: str) -> tuple[list[CsvRow], int]:
    document = read_rollout_csv(LOCKDOWN_CSV)
    rows = document.active_rows()
    seen: dict[str, int] = {}
    result: list[CsvRow] = []

    for row in rows:
        require_fields(row, ["vm", "forgejo", "full_name"], command="create-repos")
        login = row.raw["forgejo"].strip()
        if login.casefold() == owner.casefold():
            continue
        key = login.casefold()
        if key in seen:
            raise ValueError(
                f"Duplicate Forgejo login in {LOCKDOWN_CSV}: {login} "
                f"(lines {seen[key]} and {row.line_no})"
            )
        seen[key] = row.line_no
        result.append(row)
    return result, len(rows) - len(result)


def _repo_name(exam: str, login: str) -> str:
    return f"{exam}_{login}"


def _forgejo_settings() -> tuple[str, str]:
    cfg = load_config()
    return f"https://{cfg.forgejo_host}", cfg.forgejo_exam_owner


def _checked_client(base_url: str, owner: str) -> ForgejoClient:
    token = get_token()
    if not token:
        raise ValueError("Forgejo token is empty")
    client = ForgejoClient(base_url, token)
    current = client.current_user()
    login = str(current.get("login") or current.get("username") or "").strip()
    if login.casefold() != owner.casefold():
        raise RuntimeError(
            f"Forgejo token belongs to {login or '<unknown>'}, but exam repositories "
            f"are managed below {owner}. Use the {owner} token."
        )
    return client


def create_repos(*, dry_run: bool) -> int:
    base_url, owner = _forgejo_settings()
    exam_repo = _exam_repo()
    exam = exam_repo.name
    students, skipped_owner = _student_rows(owner)

    print(f"Exam repository : {exam_repo}")
    print(f"Exam designation: {exam}")
    print(f"Rollout mapping  : {LOCKDOWN_CSV}")
    print(f"Forgejo          : {base_url}")
    print(f"Owner            : {owner}")
    print(f"Student repos    : {len(students)}")
    if skipped_owner:
        print(f"Teacher rows     : {skipped_owner} skipped ({owner})")

    if dry_run:
        print("\nDry run; no Forgejo requests will be made:")
        for row in students:
            login = row.raw["forgejo"].strip()
            name = _repo_name(exam, login)
            print(f"  CREATE private {owner}/{name}  <-  {row.vm} / {row.raw['full_name'].strip()}")
            print(f"         {base_url.rstrip('/')}/{owner}/{name}.git")
        return 0

    client = _checked_client(base_url, owner)

    created = 0
    existing = 0
    for row in students:
        student = row.raw["forgejo"].strip()
        name = _repo_name(exam, student)
        full_name = row.raw["full_name"].strip()
        print(f"==> {owner}/{name} ({full_name})")

        repo = client.get_repo(owner, name)
        if repo is not None:
            if not bool(repo.get("private", False)):
                raise RuntimeError(
                    f"Existing repository {owner}/{name} is not private; refusing to continue"
                )
            print("    exists, private -- keep")
            existing += 1
            continue

        client.create_repo(
            name=name,
            description=f"{exam} - Prüfungsrepo für {full_name} ({student})",
        )
        print("    created private, empty repository")
        created += 1

    print(f"\nDone: {created} created, {existing} already existed.")
    print("No collaborators were granted and no Git content was pushed.")
    return 0


def change_access(*, action: str, dry_run: bool) -> int:
    base_url, owner = _forgejo_settings()
    exam_repo = _exam_repo()
    exam = exam_repo.name
    students, skipped_owner = _student_rows(owner)

    print(f"Exam repository : {exam_repo}")
    print(f"Exam designation: {exam}")
    print(f"Rollout mapping  : {LOCKDOWN_CSV}")
    print(f"Forgejo          : {base_url}")
    print(f"Owner            : {owner}")
    print(f"Action           : {action}")
    print(f"Student repos    : {len(students)}")
    if skipped_owner:
        print(f"Teacher rows     : {skipped_owner} skipped ({owner})")

    if dry_run:
        print("\nDry run; no Forgejo requests will be made:")
        for row in students:
            login = row.raw["forgejo"].strip()
            name = _repo_name(exam, login)
            verb = "GRANT write" if action == "grant" else "REVOKE"
            print(f"  {verb:11} {login} -> {owner}/{name}")
        return 0

    client = _checked_client(base_url, owner)
    changed = 0
    already = 0
    for row in students:
        login = row.raw["forgejo"].strip()
        name = _repo_name(exam, login)
        repo = client.get_repo(owner, name)
        if repo is None:
            raise RuntimeError(
                f"Missing exam repository {owner}/{name}; run create-repos first"
            )
        if not bool(repo.get("private", False)):
            raise RuntimeError(
                f"Existing repository {owner}/{name} is not private; refusing to continue"
            )

        if action == "grant":
            print(f"==> grant write: {login} -> {owner}/{name}")
            client.add_collaborator(owner, name, login)
            changed += 1
        elif action == "revoke":
            print(f"==> revoke: {login} -> {owner}/{name}")
            if client.remove_collaborator(owner, name, login):
                changed += 1
            else:
                print("    already absent")
                already += 1
        else:
            raise AssertionError(action)

    if action == "grant":
        print(f"\nDone: write access granted/confirmed for {changed} students.")
    else:
        print(f"\nDone: {changed} revoked, {already} already absent.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage per-student Forgejo repositories for lockdown exams."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser(
        "create-repos",
        help="Create one private empty repository per student in rollout-lockdown.csv.",
    )
    create.add_argument("--dry-run", action="store_true")

    grant = sub.add_parser(
        "grant",
        help="Grant each student write access only to their personal exam repository.",
    )
    grant.add_argument("--dry-run", action="store_true")

    revoke = sub.add_parser(
        "revoke",
        help="Remove each student's access to their personal exam repository.",
    )
    revoke.add_argument("--dry-run", action="store_true")
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create-repos":
            return create_repos(dry_run=args.dry_run)
        if args.command in {"grant", "revoke"}:
            return change_access(action=args.command, dry_run=args.dry_run)
        raise AssertionError(f"Unhandled command: {args.command}")
    except (FileNotFoundError, ValueError, RuntimeError, ForgejoError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
