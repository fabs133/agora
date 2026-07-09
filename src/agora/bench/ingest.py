"""Ingest a completed run directory into keyed matrix rows (capability-program L1-B).

Bridges the campaign harness's JSONL output (the source of truth) to the matrix
store. Loads every run/task record from ``output_dir`` (campaign or single-run
layout), STAMPS the bench-known ``model_digest`` + ``battery_version`` onto any
record that lacks them (the probe runner records neither today — the digest is
captured once by ``agora bench``, not per subprocess), builds the layer-1 frames,
and derives the keyed rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from agora.bench.matrix import derive_matrix_rows
from agora.observe.analysis import build_runs_df, build_tasks_df
from agora.observe.jsonl import RunRecord


def _stamp(record: RunRecord, model_digest: str, battery_version: str) -> RunRecord:
    """Fill only the missing key fields — a record that already recorded its own
    digest/battery wins (that provenance is stronger than the caller's stamp)."""
    updates: dict[str, Any] = {}
    if not record.model_digest and model_digest:
        updates["model_digest"] = model_digest
    if not record.battery_version and battery_version:
        updates["battery_version"] = battery_version
    return record.model_copy(update=updates) if updates else record


def ingest_run_dir(
    output_dir: str | Path,
    *,
    model_digest: str,
    battery_version: str,
    hardware: str = "",
    source: str = "local",
) -> pd.DataFrame:
    """Derive keyed capability-matrix rows from a completed run directory.

    ``model_digest`` / ``battery_version`` are the values ``agora bench`` captured
    for this run; they stamp records that did not record their own. A record that
    still has an incomplete key after stamping is rejected by
    :func:`~agora.bench.matrix.derive_matrix_rows` (never silently defaulted).
    """
    from agora.observe.analysis import load_run_records

    runs, tasks, plan = load_run_records(output_dir)
    stamped = [_stamp(r, model_digest, battery_version) for r in runs]
    runs_df = build_runs_df(stamped, plan=plan, campaign_name="bench")
    tasks_df = build_tasks_df(tasks, runs_df)
    return derive_matrix_rows(
        stamped, tasks_df, runs_df, hardware=hardware, source=source
    )
