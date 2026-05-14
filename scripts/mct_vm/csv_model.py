from __future__ import annotations

import csv
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

VM_RE = re.compile(r"^bunny[0-9][0-9]$")

CANONICAL_FIELDS = ["pcname", "vm", "forgejo", "name", "email", "file", "sha256"]

ALIASES = {
    "pc": "pcname",
    "pcname": "pcname",
    "host": "pcname",
    "computer": "pcname",
    "vm": "vm",
    "forgejo": "forgejo",
    "login": "forgejo",
    "fullname": "name",
    "full_name": "name",
    "name": "name",
    "email": "email",
    "file": "file",
    "filename": "file",
    "sha": "sha256",
    "checksum": "sha256",
    "sha256": "sha256",
}


@dataclass
class CsvRow:
    line_no: int
    raw: dict[str, str]
    is_commented: bool

    @property
    def vm(self) -> str:
        return self.raw.get("vm", "").strip()

    @property
    def is_vm_row(self) -> bool:
        return bool(VM_RE.fullmatch(self.vm))

    @property
    def is_active(self) -> bool:
        return self.is_vm_row and not self.is_commented


@dataclass
class CsvDocument:
    path: Path
    rows: list[CsvRow]

    def active_rows(self) -> list[CsvRow]:
        rows = [row for row in self.rows if row.is_active]
        _ensure_unique_vms(rows, scope="active rows")
        return rows

    def all_vm_rows(self) -> list[CsvRow]:
        rows = [row for row in self.rows if row.is_vm_row]
        _ensure_unique_vms(rows, scope="all VM rows")
        return rows

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=str(self.path.parent),
            text=True,
        )
        tmp_path = Path(tmp_name)

        try:
            with open(fd, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CANONICAL_FIELDS)
                writer.writeheader()

                for row in self.rows:
                    out = {field: row.raw.get(field, "") for field in CANONICAL_FIELDS}

                    if row.is_commented:
                        if out["pcname"]:
                            out["pcname"] = "#" + out["pcname"].lstrip("#")
                        else:
                            out["pcname"] = "#"

                    writer.writerow(out)

            tmp_path.replace(self.path)
        except Exception:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise


def _ensure_unique_vms(rows: list[CsvRow], *, scope: str) -> None:
    seen: dict[str, int] = {}
    for row in rows:
        vm = row.vm
        if vm in seen:
            raise ValueError(
                f"Duplicate VM in {scope}: {vm} "
                f"(lines {seen[vm]} and {row.line_no})"
            )
        seen[vm] = row.line_no


def _canonical_header(header: list[str]) -> list[str]:
    result: list[str] = []
    for cell in header:
        key = cell.strip().lower()
        result.append(ALIASES.get(key, key))
    return result


def _looks_like_header(row: list[str]) -> bool:
    if not row:
        return False

    first = row[0].strip().lower().lstrip("#").strip()
    return first in {"pc", "pcname", "host", "computer"}


def read_rollout_csv(path: str | Path) -> CsvDocument:
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    rows: list[CsvRow] = []

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header: list[str] | None = None

        for line_no, row in enumerate(reader, start=1):
            if not row or all(not cell.strip() for cell in row):
                continue

            first_cell = row[0].strip()
            is_commented = first_cell.startswith("#")

            header_candidate = row.copy()
            if is_commented:
                header_candidate[0] = header_candidate[0].lstrip("#").strip()

            if header is None and _looks_like_header(header_candidate):
                header = _canonical_header(header_candidate)
                continue

            if header is None:
                # Allows simple header-less CSVs in canonical order.
                header = CANONICAL_FIELDS.copy()

            padded = row + [""] * max(0, len(header) - len(row))
            raw: dict[str, str] = {}

            for idx, key in enumerate(header):
                if key not in CANONICAL_FIELDS:
                    continue

                value = padded[idx].strip()

                if idx == 0 and value.startswith("#"):
                    value = value.lstrip("#").strip()

                raw[key] = value

            for field in CANONICAL_FIELDS:
                raw.setdefault(field, "")

            rows.append(CsvRow(line_no=line_no, raw=raw, is_commented=is_commented))

    return CsvDocument(path=csv_path, rows=rows)


def require_fields(row: CsvRow, fields: Iterable[str], *, command: str) -> None:
    missing = [field for field in fields if not row.raw.get(field, "").strip()]
    if missing:
        raise ValueError(
            f"{command}: row {row.line_no} ({row.vm or 'no vm'}): "
            f"missing required field(s): {', '.join(missing)}"
        )
