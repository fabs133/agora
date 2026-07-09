"""The capability matrix — canonical CSV store (capability-program L1-A).

DERIVED, rebuildable index over campaign JSONL (the source of truth). One row per
``(key, cell, date)`` where key = :data:`~agora.bench.keys.KEY_FIELDS` and cell =
``(model, strategy, sub_target)``. The vector math is
:func:`agora.observe.layer2.capability_vectors`; this module PARTITIONS runs by
key (crucially: a battery's two harness arms have different ``harness_hash`` and
so must be derived separately) and attaches the full key to each vector row.

CSV is the canonical, git-diffable, re-derivable format (owner decision §5.1). An
optional local SQLite may later be built FROM the CSV for query speed, but the
CSV is what crosses the Layer-1/Layer-2 boundary.

Comparability is enforced at query time: :func:`query` refuses to return rows
spanning more than one distinct key unless ``allow_cross_key=True`` — never a
silent pool.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from agora.bench.keys import KEY_FIELDS, harness_hash
from agora.observe.layer2 import AXIS_TOOL_CALL_FIDELITY, capability_vectors

#: Value columns carried verbatim from the layer-2 capability vector.
_VECTOR_VALUE_COLUMNS: tuple[str, ...] = (
    "raw_value",
    "normalized_score",
    "repeats",
    "excluded_repeats",
    "ci_low",
    "ci_high",
)

#: The full canonical CSV schema, in column order.
MATRIX_COLUMNS: tuple[str, ...] = (
    # --- re-derivable key (comparability) ---
    *KEY_FIELDS,
    # --- cell identity ---
    "model",
    "strategy",
    "axis",
    "sub_target",
    # --- value ---
    *_VECTOR_VALUE_COLUMNS,
    # --- attestation metadata (not part of the key) ---
    "hardware",
    "date",
    "git_commit",
    # --- provenance ---
    "source",
)

#: A row's identity for idempotent append / rebuild: key + cell + date. Re-deriving
#: the same run replaces its rows; a later bench (new date) adds rows (owner §5.4).
ROW_ID_COLUMNS: tuple[str, ...] = (*KEY_FIELDS, "model", "strategy", "sub_target", "date")


def _rget(rec: Any, field: str, default: Any = None) -> Any:
    """Read ``field`` from a RunRecord (attribute) or a raw JSONL dict."""
    if isinstance(rec, dict):
        return rec.get(field, default)
    return getattr(rec, field, default)


def _record_key(rec: Any) -> tuple[str, str, Any, str, str]:
    """The five key values for one run record (daemon_version = ollama_version)."""
    return (
        str(_rget(rec, "model_digest", "") or ""),
        str(_rget(rec, "battery_version", "") or ""),
        _rget(rec, "probe_version", None),
        harness_hash(_rget(rec, "harness", None)),
        str(_rget(rec, "ollama_version", "") or ""),
    )


def _date_of(rec: Any) -> str:
    """UTC calendar date (YYYY-MM-DD) from a record's ``started_at`` ISO string."""
    started = str(_rget(rec, "started_at", "") or "")
    return started[:10]  # ISO-8601 date prefix; "" if unset


def derive_matrix_rows(
    run_records: list[Any],
    tasks_df: pd.DataFrame,
    runs_df: pd.DataFrame,
    *,
    axis: str = AXIS_TOOL_CALL_FIDELITY,
    hardware: str = "",
    source: str = "local",
) -> pd.DataFrame:
    """Derive keyed matrix rows from a set of run records + their layer-1 frames.

    Partitions ``run_records`` by the full key (so the two harness arms of a
    battery never collapse into one cell), runs :func:`capability_vectors` on
    each partition's runs, and stamps the key + metadata onto every vector row.
    A run record with an incomplete key (missing digest, probe_version, or
    daemon_version) is REJECTED — the matrix never carries an un-comparable row.
    """
    if "run_id" not in runs_df.columns or "run_id" not in tasks_df.columns:
        raise ValueError("derive_matrix_rows: runs_df and tasks_df must carry a run_id column")

    partitions: dict[tuple, list[Any]] = {}
    for rec in run_records:
        key = _record_key(rec)
        _reject_incomplete_key(rec, key)
        partitions.setdefault(key, []).append(rec)

    out_rows: list[dict[str, Any]] = []
    for key, recs in partitions.items():
        run_ids = {str(_rget(r, "run_id")) for r in recs}
        t_sub = tasks_df[tasks_df["run_id"].astype("string").isin(run_ids)]
        r_sub = runs_df[runs_df["run_id"].astype("string").isin(run_ids)]
        if t_sub.empty:
            continue
        vectors = capability_vectors(t_sub, r_sub, axis=axis, campaign="")
        model_digest, battery_version, probe_version, harness_h, daemon_version = key
        # Metadata is constant per key-partition (one model, one bench); take the
        # earliest record deterministically so rebuild is idempotent.
        lead = min(recs, key=lambda r: str(_rget(r, "started_at", "") or ""))
        meta = {
            "hardware": hardware or str(_rget(lead, "host", "") or ""),
            "date": _date_of(lead),
            "git_commit": str(_rget(lead, "git_commit", "") or ""),
            "source": source,
        }
        for _, v in vectors.iterrows():
            out_rows.append(
                {
                    "model_digest": model_digest,
                    "battery_version": battery_version,
                    "probe_version": probe_version,
                    "harness_hash": harness_h,
                    "daemon_version": daemon_version,
                    "model": v["model"],
                    "strategy": v["strategy"],
                    "axis": v["axis"],
                    "sub_target": v["sub_target"],
                    **{c: v[c] for c in _VECTOR_VALUE_COLUMNS},
                    **meta,
                }
            )
    return _as_matrix_frame(out_rows)


def _reject_incomplete_key(rec: Any, key: tuple) -> None:
    model_digest, battery_version, probe_version, _hh, daemon_version = key
    missing = [
        name
        for name, val in (
            ("model_digest", model_digest),
            ("battery_version", battery_version),
            ("probe_version", probe_version),
            ("daemon_version", daemon_version),
        )
        if val in (None, "", "unknown")
    ]
    if missing:
        rid = _rget(rec, "run_id", "?")
        raise ValueError(
            f"run {rid!r} cannot enter the matrix: incomplete key field(s) {missing}. "
            f"A matrix row must be re-derivable; bench runs must record all key fields."
        )


def _as_matrix_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=list(MATRIX_COLUMNS))
    return df.reindex(columns=list(MATRIX_COLUMNS))


# ------------------------------------------------------------------ store I/O


def load_matrix(path: str) -> pd.DataFrame:
    """Load the canonical CSV, or an empty typed frame when it does not exist."""
    from pathlib import Path

    if not Path(path).exists():
        return pd.DataFrame(columns=list(MATRIX_COLUMNS))
    return pd.read_csv(path).reindex(columns=list(MATRIX_COLUMNS))


def _sort_key(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(list(ROW_ID_COLUMNS), kind="stable").reset_index(drop=True)


def append_rows(path: str, new_rows: pd.DataFrame) -> pd.DataFrame:
    """Merge ``new_rows`` into the CSV at ``path``, idempotent on
    :data:`ROW_ID_COLUMNS` (re-deriving the same run replaces, never duplicates).
    Writes the sorted result and returns it."""
    existing = load_matrix(path)
    combined = pd.concat([existing, new_rows], ignore_index=True)
    # Last write wins per row-id (a re-derivation supersedes the prior one).
    combined = combined.drop_duplicates(subset=list(ROW_ID_COLUMNS), keep="last")
    combined = _sort_key(combined)
    combined.to_csv(path, index=False)
    return combined


def rebuild(path: str, rows: pd.DataFrame) -> pd.DataFrame:
    """Rewrite the CSV from ``rows`` alone (no merge) — the from-scratch rebuild.
    Deterministic: same input rows produce a byte-identical CSV."""
    out = _sort_key(rows.drop_duplicates(subset=list(ROW_ID_COLUMNS), keep="last"))
    out = out.reindex(columns=list(MATRIX_COLUMNS))
    out.to_csv(path, index=False)
    return out


# ------------------------------------------------------------------ query


def query(matrix: pd.DataFrame, *, allow_cross_key: bool = False, **filters: Any) -> pd.DataFrame:
    """Filter the matrix by equality on any column(s). Comparability guard: if the
    result spans more than one distinct key tuple, raise unless
    ``allow_cross_key=True`` — never silently pool across keys."""
    result = matrix
    for col, val in filters.items():
        if col not in matrix.columns:
            raise KeyError(f"query: unknown column {col!r}")
        result = result[result[col] == val]
    if not allow_cross_key and not result.empty:
        distinct_keys = result[list(KEY_FIELDS)].drop_duplicates()
        if len(distinct_keys) > 1:
            raise ValueError(
                f"query spans {len(distinct_keys)} distinct keys — comparing across "
                f"(model_digest, battery, probe, harness, daemon) is not valid without "
                f"allow_cross_key=True. Narrow the filters or opt in explicitly."
            )
    return result.reset_index(drop=True)
