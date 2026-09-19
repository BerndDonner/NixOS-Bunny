from __future__ import annotations

import json
from pathlib import Path

from .csv_model import read_rollout_csv, require_fields


def _nix_string(value: str) -> str:
    """Return a safely quoted Nix string."""
    # JSON string syntax is accepted for ordinary Nix strings and correctly
    # escapes quotes, backslashes and control characters.
    return json.dumps(value, ensure_ascii=False)


def generate_nix(*, csv_path: str, target_dir: str, dry_run: bool = False) -> int:
    doc = read_rollout_csv(csv_path)
    rows = doc.active_rows()

    if not rows:
        print(f"WARN:  No active VM rows found in {csv_path}")
        return 0

    # Validate everything before touching the target directory. This keeps a
    # malformed CSV from deleting otherwise usable host definitions.
    hosts: list[tuple[str, str]] = []
    for row in rows:
        require_fields(
            row,
            ["vm", "course", "forgejo", "full_name", "email"],
            command="generate-nix",
        )

        vm = row.vm
        course = row.raw["course"].strip()
        forgejo = row.raw["forgejo"].strip()
        full_name = row.raw["full_name"].strip()
        email = row.raw["email"].strip()

        content = (
            "{\n"
            f"  gitName  = {_nix_string(full_name)};\n"
            f"  gitEmail = {_nix_string(email)};\n"
            f"  forgejo  = {_nix_string(forgejo)};\n"
            f"  course   = {_nix_string(course)};\n"
            "}\n"
        )
        hosts.append((vm, content))

    out_dir = Path(target_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    wanted = {f"{vm}.nix" for vm, _content in hosts}

    # Host files are generated data. Remove bunnyXX definitions that are no
    # longer active in the rollout CSV, while keeping bunny.nix/default.nix and
    # any unrelated files intact.
    for old_path in sorted(out_dir.glob("bunny[0-9][0-9].nix")):
        if old_path.name not in wanted:
            if dry_run:
                print(f"Would remove stale {old_path}")
            else:
                old_path.unlink()
                print(f"Removed stale {old_path}")

    for vm, content in hosts:
        out_path = out_dir / f"{vm}.nix"
        if dry_run:
            print(f"Would write {out_path}")
        else:
            out_path.write_text(content, encoding="utf-8")
            print(f"Wrote {out_path}")

    return 0
