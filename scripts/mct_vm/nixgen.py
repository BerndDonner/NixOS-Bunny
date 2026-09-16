from __future__ import annotations

import json
from pathlib import Path

from .csv_model import read_rollout_csv, require_fields


def _nix_string(value: str) -> str:
    """Return a safely quoted Nix string."""
    # JSON string syntax is accepted for ordinary Nix strings and correctly
    # escapes quotes, backslashes and control characters.
    return json.dumps(value, ensure_ascii=False)


def generate_nix(*, csv_path: str, target_dir: str) -> int:
    doc = read_rollout_csv(csv_path)
    rows = doc.all_vm_rows()

    if not rows:
        print("WARN:  No VM rows found in rollout.csv")
        return 0

    out_dir = Path(target_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for row in rows:
        require_fields(row, ["vm", "forgejo", "name", "email"], command="generate-nix")

        vm = row.vm
        forgejo = row.raw["forgejo"].strip()
        name = row.raw["name"].strip()
        email = row.raw["email"].strip()

        content = (
            "{\n"
            f"  gitName  = {_nix_string(name)};\n"
            f"  gitEmail = {_nix_string(email)};\n"
            f"  forgejo  = {_nix_string(forgejo)};\n"
            "}\n"
        )

        out_path = out_dir / f"{vm}.nix"
        out_path.write_text(content, encoding="utf-8")
        print(f"Wrote {out_path}")

    return 0
