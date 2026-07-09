"""Index builder: submissions -> matrix.csv + conflicts.md (capability-program L2-2 core).

Pure logic, in the agora package so the exchange CI just CALLS it (shared code,
no drift). Aggregates every submission's manifest rows per (key, cell):

- **Reproduction count.** Independent submissions at the same key+cell whose
  value AGREES raise that row's ``reproductions``. n=1 is a valid row.
- **Conflict detection.** When submissions at one key+cell DISAGREE, the cell is
  surfaced in ``conflicts.md`` — never silently averaged. The matrix keeps the
  majority (modal) value flagged ``conflicted=True`` so no evidence is lost and
  consumers see the cell is contested.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from agora.bench.keys import KEY_FIELDS
from agora.exchange.schema import Attestation, Manifest

#: A cell's identity (the key + which vector cell). Reproductions accrue per cell.
_CELL = (*KEY_FIELDS, "model", "strategy", "sub_target")
_VALUE_FIELDS = ("raw_value", "normalized_score", "repeats", "excluded_repeats", "ci_low", "ci_high")

INDEX_COLUMNS: tuple[str, ...] = (
    *_CELL,
    "axis",
    *_VALUE_FIELDS,
    "date",
    "reproductions",
    "contributors",
    "conflicted",
)

_TOL = 6  # decimal places at which two values are considered to agree


@dataclass
class IndexResult:
    matrix: pd.DataFrame
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def load_submission(directory: str | Path) -> tuple[Manifest, Attestation]:
    """Load a submission's manifest + attestation from its directory."""
    import yaml

    d = Path(directory)
    manifest = Manifest.model_validate(yaml.safe_load((d / "manifest.yaml").read_text(encoding="utf-8")))
    attestation = Attestation.model_validate(
        yaml.safe_load((d / "attestation.yaml").read_text(encoding="utf-8"))
    )
    return manifest, attestation


def _cluster_key(raw_value: Any) -> Any:
    """Values agree when they round to the same figure at :data:`_TOL`. NaN forms
    its own cluster (an unmeasured cell never 'agrees' with a measured one)."""
    if raw_value is None:
        return "nan"
    try:
        v = float(raw_value)
    except (TypeError, ValueError):
        return str(raw_value)
    return "nan" if math.isnan(v) else round(v, _TOL)


def build_index(submission_dirs: list[str | Path]) -> IndexResult:
    """Aggregate submissions into the derived index (reproductions + conflicts)."""
    # cell -> {cluster_key -> [record...]}
    cells: dict[tuple, dict[Any, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for directory in submission_dirs:
        manifest, attestation = load_submission(directory)
        for row in manifest.rows:
            rec = row.model_dump()
            rec["contributor"] = attestation.contributor or "anon"
            cell = tuple(rec[k] for k in _CELL)
            cells[cell][_cluster_key(rec["raw_value"])].append(rec)

    out_rows: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for cell, clusters in cells.items():
        conflicted = len(clusters) > 1
        # Winning cluster = most reproductions; ties broken deterministically.
        winner_key = max(clusters, key=lambda k: (len(clusters[k]), _sortable(k)))
        winner = clusters[winner_key]
        rep = winner[0]
        out_rows.append(
            {
                **{k: rep[k] for k in _CELL},
                "axis": rep.get("axis"),
                **{f: rep.get(f) for f in _VALUE_FIELDS},
                "date": rep.get("date"),
                "reproductions": len(winner),
                "contributors": ",".join(sorted({r["contributor"] for r in winner})),
                "conflicted": conflicted,
            }
        )
        if conflicted:
            conflicts.append(
                {
                    "cell": dict(zip(_CELL, cell, strict=True)),
                    "clusters": [
                        {
                            "raw_value": recs[0]["raw_value"],
                            "count": len(recs),
                            "contributors": sorted({r["contributor"] for r in recs}),
                        }
                        for _, recs in sorted(clusters.items(), key=lambda kv: -len(kv[1]))
                    ],
                }
            )

    matrix = (
        pd.DataFrame(out_rows).reindex(columns=list(INDEX_COLUMNS))
        if out_rows
        else pd.DataFrame(columns=list(INDEX_COLUMNS))
    )
    if not matrix.empty:
        matrix = matrix.sort_values(list(_CELL), kind="stable").reset_index(drop=True)
    return IndexResult(matrix=matrix, conflicts=conflicts)


def _sortable(cluster_key: Any) -> Any:
    return (0, cluster_key) if isinstance(cluster_key, (int, float)) else (1, str(cluster_key))


def render_conflicts(conflicts: list[dict[str, Any]]) -> str:
    """Render conflicts.md — disagreeing reproductions, never averaged away."""
    if not conflicts:
        return "# Conflicts\n\nNone — every reproduced cell agrees.\n"
    lines = ["# Conflicts", "", "Cells where independent submissions disagree "
             "(surfaced, never averaged):", ""]
    for c in conflicts:
        cell = c["cell"]
        lines.append(
            f"## {cell['model']}  {cell['sub_target']}  "
            f"[digest={cell['model_digest']} harness={cell['harness_hash']}]"
        )
        for cl in c["clusters"]:
            lines.append(f"- value {cl['raw_value']} x{cl['count']} — {', '.join(cl['contributors'])}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_index(dest: str | Path, result: IndexResult) -> Path:
    """Write index/matrix.csv + index/conflicts.md under ``dest``. Returns index dir."""
    index_dir = Path(dest) / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    result.matrix.to_csv(index_dir / "matrix.csv", index=False)
    (index_dir / "conflicts.md").write_text(render_conflicts(result.conflicts), encoding="utf-8")
    return index_dir


__all__ = ["INDEX_COLUMNS", "IndexResult", "build_index", "load_submission", "render_conflicts", "write_index"]
