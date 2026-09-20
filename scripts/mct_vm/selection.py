from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Iterable

from .csv_model import CANONICAL_FIELDS, CsvRow


def _compile_pattern(pattern: str) -> str:
    """Return a case-folded glob where only '*' and '?' are special."""
    # fnmatch also supports character classes such as [0-9].  The rollout
    # selectors intentionally expose only the two simple shell wildcards, so
    # make '[' literal before handing the pattern to fnmatchcase().
    return pattern.casefold().replace("[", "[[]")


def row_match_values(row: CsvRow) -> tuple[str, ...]:
    """Values a selector may match for one rollout CSV row."""
    values = [row.raw.get(field, "").strip() for field in CANONICAL_FIELDS]
    full_name = row.raw.get("full_name", "").strip()
    if full_name:
        values.extend(full_name.split())

    # Preserve CSV field order but avoid testing the same value repeatedly.
    return tuple(dict.fromkeys(value for value in values if value))


def row_matches(row: CsvRow, pattern: str) -> bool:
    compiled = _compile_pattern(pattern.strip())
    return any(
        fnmatchcase(value.casefold(), compiled)
        for value in row_match_values(row)
    )


def select_rows(
    rows: Iterable[CsvRow],
    *,
    include: Iterable[str],
    exclude: Iterable[str],
) -> list[CsvRow]:
    """Select rows using ORed includes followed by ORed excludes.

    An empty include set selects nothing.  A row is included when at least one
    include pattern matches one of its CSV values, and excluded when at least
    one exclude pattern matches.  Each input row can appear at most once in the
    result, no matter how many patterns match it.
    """
    include_patterns = tuple(include)
    exclude_patterns = tuple(exclude)
    if not include_patterns:
        return []

    selected: list[CsvRow] = []
    for row in rows:
        if not any(row_matches(row, pattern) for pattern in include_patterns):
            continue
        if any(row_matches(row, pattern) for pattern in exclude_patterns):
            continue
        selected.append(row)
    return selected
