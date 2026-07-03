"""Pivot two axis-1 capability_vectors.csv into a v1 / v2-control / v2-treatment table.

Pure read -> transform -> print. Groups by (model, sub_target) and pivots the
campaign/strategy dimension into three columns: v1-steady (v1 raw_value — the v1
CSV already encodes steady-state via the reproducibility/``excluded_repeats``
override, one row per cell), v2-control (strategy null), v2-treatment (strategy
set; models with no treatment arm emit ``—``).

Usage:  python scripts/compare_campaigns.py <v1_csv> <v2_csv>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any

MISSING = "—"


def _fmt(value: str | None) -> str:
    if not value:
        return MISSING
    try:
        return f"{float(value):.3g}"
    except ValueError:
        return str(value)


def _index(rows: list[dict[str, Any]], *, treatment: bool | None = None) -> dict:
    """(model, sub_target) -> raw_value. ``treatment`` None=all, True=strategy-set,
    False=null-strategy (control)."""
    out: dict[tuple[str, str], str] = {}
    for r in rows:
        has_strategy = bool((r.get("strategy") or "").strip())
        if treatment is True and not has_strategy:
            continue
        if treatment is False and has_strategy:
            continue
        out[(r["model"], r["sub_target"])] = r.get("raw_value", "")
    return out


def build_table(v1_rows: list[dict[str, Any]], v2_rows: list[dict[str, Any]]) -> str:
    """Render the three-column comparison markdown table."""
    v1 = _index(v1_rows)  # v1 is all-control (strategy null)
    control = _index(v2_rows, treatment=False)
    treatment = _index(v2_rows, treatment=True)
    lines = [
        "| model | sub_target | v1-steady | v2-control | v2-treatment |",
        "|---|---|---|---|---|",
    ]
    for key in sorted(set(v1) | set(control) | set(treatment)):
        lines.append(
            f"| {key[0]} | {key[1]} | {_fmt(v1.get(key))} | "
            f"{_fmt(control.get(key))} | {_fmt(treatment.get(key))} |"
        )
    return "\n".join(lines)


def _read_csv(path: str | Path) -> list[dict[str, Any]]:
    return list(csv.DictReader(Path(path).read_text(encoding="utf-8").splitlines()))


def main(argv: list[str] | None = None) -> int:
    # Output contains the em-dash placeholder; Windows redirected stdout defaults
    # to cp1252 and would mojibake it. Force UTF-8 so `> file` stays clean.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: compare_campaigns.py <v1_csv> <v2_csv>", file=sys.stderr)
        return 2
    print(build_table(_read_csv(argv[0]), _read_csv(argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
