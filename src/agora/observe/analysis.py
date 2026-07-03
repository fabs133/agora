"""Layer-1 analysis pipeline: load campaign JSONL into tidy pandas DataFrames.

Pure data plumbing. ``load_run_records`` / ``load_campaign`` read the filesystem;
everything else is a pure function (same input → same output, no global state, no
env reads, no logging-config changes). Warnings go through :mod:`warnings` so
callers can filter them.

The three frames (``runs_df``, ``tasks_df``, ``postconditions_df``) follow the
locked schema in the axis-1 design notes. Nullable pandas dtypes (``Int64``,
``boolean``, ``string``) are used so ``None`` survives round-trips and downstream
filtering behaves.

Requires the ``analysis`` optional dependency group (``pip install -e
.[analysis]``) for pandas.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Any

import pandas as pd

from agora.core.errors import AgoraError
from agora.observe.jsonl import PlanEntry, RunRecord, TaskRecord

__all__ = [
    "build_postconditions_df",
    "build_runs_df",
    "build_tasks_df",
    "load_campaign",
    "load_run_records",
    "model_family",
    "model_size_b",
]


# ------------------------------------------------------------------ derived helpers


def model_family(model: str) -> str:
    """Classify a profile model string into a coarse family.

    Pure — never raises. Unknown strings return ``"unknown"`` (callers warn
    once per distinct string; see :func:`build_runs_df`).
    """
    m = (model or "").lower()
    if m.startswith("ollama/qwen2.5-coder:"):
        return "qwen-coder"
    if m.startswith("ollama/qwen2.5:") and "instruct" in m:
        return "qwen-instruct"
    if m.startswith("ollama/qwen3:"):
        return "qwen-thinking"
    if m.startswith("ollama/gemma"):
        return "gemma"
    if m.startswith("ollama/mistral"):
        return "mistral"
    return "unknown"


#: ``:7b`` / ``:14b`` / ``e4b`` style size tokens. The char before the digits is
#: either ``:`` (``:7b``) or ``e`` (``e4b``); ``\b`` after ``b`` so ``7b-instruct``
#: still parses. Case-insensitive.
_SIZE_RE = re.compile(r"[:e](\d+(?:\.\d+)?)b\b", re.IGNORECASE)


def model_size_b(model: str) -> float:
    """Parse the parameter count (in billions) from a model string.

    ``:7b`` / ``:14b`` / ``:30b`` / ``:12b`` → that number; ``e4b`` → 4.0;
    ``7b-instruct`` → 7.0. Unparseable → ``float('nan')`` (callers warn once
    per distinct string). Pure — never raises.
    """
    match = _SIZE_RE.search(model or "")
    if match is None:
        return float("nan")
    return float(match.group(1))


# ------------------------------------------------------------------ schemas

#: Ordered column → pandas dtype. ``"datetime"`` is a sentinel handled specially
#: (parsed via ``pd.to_datetime(..., utc=True)``).
_RUNS_SCHEMA: dict[str, str] = {
    "run_id": "string",
    "campaign_run_id": "string",
    "campaign_name": "string",
    "started_at": "datetime",
    "ended_at": "datetime",
    "duration_s": "float64",
    "probe_name": "string",
    "flow_path": "string",
    "project_name": "string",
    "model": "string",
    "profile_name": "string",
    "model_family": "string",
    "model_size_b": "float64",
    "num_ctx": "Int64",
    "max_tokens": "Int64",
    "temperature": "float64",
    "seed": "Int64",
    "keep_alive": "string",
    "scaffolding": "string",
    "strictness": "string",
    "strategy": "string",
    "repeat": "Int64",
    "success": "boolean",
    "exit_code": "Int64",
    "tasks_total": "Int64",
    "tasks_passed": "Int64",
    "tasks_failed": "Int64",
    "tasks_first_pass": "Int64",
    "tasks_completed_ratio": "float64",
    "tasks_pass_ratio": "float64",
    "async_leak_hits": "Int64",
    "model_offloaded": "boolean",
    "tokens_in": "Int64",
    "tokens_out": "Int64",
    "ollama_version": "string",
    "git_commit": "string",
    "host": "string",
}

_TASKS_SCHEMA: dict[str, str] = {
    "run_id": "string",
    "campaign_run_id": "string",
    "model": "string",
    "model_family": "string",
    "model_size_b": "float64",
    "scaffolding": "string",
    "strictness": "string",
    "strategy": "string",
    "repeat": "Int64",
    "temperature": "float64",
    "seed": "Int64",
    "num_ctx": "Int64",
    "started_at": "datetime",
    "task_id": "string",
    "task_index": "Int64",
    "role": "string",
    "task_kind": "string",
    "status": "string",
    "first_pass": "boolean",
    "loopback_count": "Int64",
    "iterations": "Int64",
    "tool_calls_total": "Int64",
    "tool_calls_structured": "Int64",
    "tool_calls_text_fallback": "Int64",
    "tool_calls_malformed": "Int64",
    "tool_call_unknown_name": "Int64",
    "turns_with_text_fallback": "Int64",
    "first_text_fallback_iteration": "Int64",
    "failure_category": "string",
    "failure_detail": "string",
    "duration_s": "float64",
}

#: Run-level columns joined onto each task row (keyed by run_id).
_TASK_JOIN_COLUMNS: tuple[str, ...] = (
    "campaign_run_id",
    "model",
    "model_family",
    "model_size_b",
    "scaffolding",
    "strictness",
    "strategy",
    "repeat",
    "temperature",
    "seed",
    "num_ctx",
    "started_at",
)

_POSTCONDITIONS_SCHEMA: dict[str, str] = {
    "run_id": "string",
    "task_id": "string",
    "postcondition_name": "string",
    "passed": "boolean",
}


def _apply_schema(rows: list[dict[str, Any]], schema: dict[str, str]) -> pd.DataFrame:
    """Build a DataFrame with exactly ``schema``'s columns, in order, typed.

    Works for empty ``rows`` — yields a zero-row frame with every column
    present and correctly typed.
    """
    df = pd.DataFrame(rows)
    df = df.reindex(columns=list(schema))
    for col, dtype in schema.items():
        if dtype == "datetime":
            # Force nanosecond resolution: pandas >= 2 infers us from
            # microsecond ISO strings, but the locked schema pins ns.
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce").astype(
                "datetime64[ns, UTC]"
            )
        else:
            df[col] = df[col].astype(dtype)
    return df


# ------------------------------------------------------------------ loading


def _load_jsonl(path: Path, model: type, *, label: str) -> list[Any]:
    """Parse every non-blank line of ``path`` into ``model``.

    Raises :class:`AgoraError` naming the file + 1-based line number on a
    validation/JSON failure — malformed records are never silently dropped.
    """
    records: list[Any] = []
    text = path.read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            records.append(model.model_validate_json(raw))
        except Exception as exc:  # noqa: BLE001 — re-raise with location context
            raise AgoraError(
                f"{label} validation failed at {path}:{lineno}: {exc}"
            ) from exc
    return records


def _load_plan(path: Path) -> list[PlanEntry]:
    return _load_jsonl(path, PlanEntry, label="plan.jsonl")


def _load_run_dir(directory: Path) -> tuple[list[RunRecord], list[TaskRecord]]:
    """Load the run.jsonl + tasks.jsonl pair from a single directory (if present)."""
    runs: list[RunRecord] = []
    tasks: list[TaskRecord] = []
    run_path = directory / "run.jsonl"
    task_path = directory / "tasks.jsonl"
    if run_path.is_file():
        runs = _load_jsonl(run_path, RunRecord, label="run.jsonl")
    if task_path.is_file():
        tasks = _load_jsonl(task_path, TaskRecord, label="tasks.jsonl")
    return runs, tasks


def load_run_records(
    output_dir: str | Path,
) -> tuple[list[RunRecord], list[TaskRecord], list[PlanEntry] | None]:
    """Walk an output directory and load every (run.jsonl, tasks.jsonl) pair.

    Returns ``(runs, tasks, plan)``. ``plan`` is the parsed ``plan.jsonl`` at the
    root when present (campaign layout), else ``None``.

    Supported layouts:

    - **Campaign** — ``<output_dir>/plan.jsonl`` plus one subdir per run (named
      by the campaign run id, e.g. ``r001``) holding ``run.jsonl`` + ``tasks.jsonl``.
      The root-level ``run.jsonl`` / ``tasks.jsonl`` (the harness's concatenations)
      are skipped so records aren't double-counted. Each plan entry is enriched
      with the ``run_id`` read from its matching subdir's ``run.jsonl``.
    - **Single-run** — ``<output_dir>/{run.jsonl, tasks.jsonl}`` (flat), or a
      single ``<output_dir>/<run_id>/`` subdir.

    Validates every line against the pydantic models; raises
    :class:`AgoraError` with the offending file path + line number on failure.
    """
    out = Path(output_dir)
    plan_path = out / "plan.jsonl"
    plan: list[PlanEntry] | None = _load_plan(plan_path) if plan_path.is_file() else None

    runs: list[RunRecord] = []
    tasks: list[TaskRecord] = []

    if plan is not None:
        # Campaign layout: iterate per-run subdirs; skip root concatenations.
        plan_by_id = {p.id: p for p in plan}
        for sub in sorted(p for p in out.iterdir() if p.is_dir()):
            sub_runs, sub_tasks = _load_run_dir(sub)
            if not sub_runs and not sub_tasks:
                continue
            runs.extend(sub_runs)
            tasks.extend(sub_tasks)
            # Enrich the matching plan entry with the run uuid found here.
            entry = plan_by_id.get(sub.name)
            if entry is not None and sub_runs:
                entry.run_id = sub_runs[-1].run_id
        return runs, tasks, plan

    # No plan: single-run. Prefer a flat pair, else a single subdir.
    if (out / "run.jsonl").is_file() or (out / "tasks.jsonl").is_file():
        runs, tasks = _load_run_dir(out)
        return runs, tasks, None

    for sub in sorted(p for p in out.iterdir() if p.is_dir()):
        sub_runs, sub_tasks = _load_run_dir(sub)
        runs.extend(sub_runs)
        tasks.extend(sub_tasks)
    return runs, tasks, None


# ------------------------------------------------------------------ builders


def _ratio(numerator: float, denominator: float) -> float:
    """numerator / denominator, or NaN when the denominator is zero."""
    if denominator == 0:
        return float("nan")
    return numerator / denominator


def build_runs_df(
    runs: list[RunRecord],
    plan: list[PlanEntry] | None = None,
    campaign_name: str | None = None,
) -> pd.DataFrame:
    """Construct ``runs_df`` (one row per run). Pure.

    ``plan`` (with ``run_id`` enriched by :func:`load_run_records`) supplies the
    ``campaign_run_id`` + ``repeat`` for each run; absent plan → both ``None``.
    Empty ``runs`` yields a typed empty frame with the locked columns.
    """
    # run_id → (campaign_run_id, repeat) from the enriched plan.
    plan_lookup: dict[str, tuple[str, int]] = {}
    if plan is not None:
        for entry in plan:
            if entry.run_id is not None:
                plan_lookup[entry.run_id] = (entry.id, entry.repeat)

    # Warn once per distinct model string that doesn't classify / parse.
    _warn_unknown_models({r.profile.model for r in runs})

    rows: list[dict[str, Any]] = []
    for r in runs:
        prof = r.profile
        campaign_run_id, repeat = plan_lookup.get(r.run_id, (None, None))
        passed, failed, total = r.tasks_passed, r.tasks_failed, r.tasks_total
        rows.append(
            {
                "run_id": r.run_id,
                "campaign_run_id": campaign_run_id,
                "campaign_name": campaign_name,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "duration_s": r.duration_s,
                "probe_name": r.probe_name,
                "flow_path": r.flow_path,
                "project_name": r.project_name,
                "model": prof.model,
                "profile_name": prof.name,
                "model_family": model_family(prof.model),
                "model_size_b": model_size_b(prof.model),
                "num_ctx": prof.num_ctx,
                "max_tokens": prof.max_tokens,
                "temperature": prof.temperature,
                "seed": prof.seed,
                "keep_alive": prof.keep_alive,
                "scaffolding": r.arm.scaffolding,
                "strictness": r.arm.strictness,
                "strategy": r.strategy,
                "repeat": repeat,
                "success": r.success,
                "exit_code": r.exit_code,
                "tasks_total": total,
                "tasks_passed": passed,
                "tasks_failed": failed,
                "tasks_first_pass": r.tasks_first_pass,
                "tasks_completed_ratio": _ratio(passed + failed, total),
                "tasks_pass_ratio": _ratio(passed, passed + failed),
                "async_leak_hits": r.async_leak_hits,
                "model_offloaded": r.model_offloaded,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "ollama_version": r.ollama_version,
                "git_commit": r.git_commit,
                "host": r.host,
            }
        )
    return _apply_schema(rows, _RUNS_SCHEMA)


def _warn_unknown_models(models: set[str]) -> None:
    """Emit one warning per distinct model string that doesn't classify/parse."""
    for m in sorted(models):
        if model_family(m) == "unknown":
            warnings.warn(f"unrecognised model family for {m!r}", stacklevel=3)
        if pd.isna(model_size_b(m)):
            warnings.warn(f"could not parse model size from {m!r}", stacklevel=3)


def build_tasks_df(
    tasks: list[TaskRecord],
    runs_df: pd.DataFrame,
) -> pd.DataFrame:
    """Construct ``tasks_df`` by joining task records to ``runs_df`` on run_id.

    Tasks whose ``run_id`` is absent from ``runs_df`` are dropped with a warning
    (run-level fields are never fabricated). Pure.
    """
    # run_id → joined run-level fields.
    run_index: dict[str, dict[str, Any]] = {}
    for _, row in runs_df.iterrows():
        run_index[row["run_id"]] = {col: row[col] for col in _TASK_JOIN_COLUMNS}

    rows: list[dict[str, Any]] = []
    orphans: list[str] = []
    for t in tasks:
        joined = run_index.get(t.run_id)
        if joined is None:
            orphans.append(t.run_id)
            continue
        rows.append(
            {
                "run_id": t.run_id,
                "campaign_run_id": joined["campaign_run_id"],
                "model": joined["model"],
                "model_family": joined["model_family"],
                "model_size_b": joined["model_size_b"],
                "scaffolding": joined["scaffolding"],
                "strictness": joined["strictness"],
                "strategy": joined["strategy"],
                "repeat": joined["repeat"],
                "temperature": joined["temperature"],
                "seed": joined["seed"],
                "num_ctx": joined["num_ctx"],
                "started_at": joined["started_at"],
                "task_id": t.task_id,
                "task_index": t.task_index,
                "role": t.role,
                "task_kind": t.task_kind,
                "status": t.status,
                "first_pass": t.first_pass,
                "loopback_count": t.loopback_count,
                "iterations": t.iterations,
                "tool_calls_total": t.tool_calls_total,
                "tool_calls_structured": t.tool_calls_structured,
                "tool_calls_text_fallback": t.tool_calls_text_fallback,
                "tool_calls_malformed": t.tool_calls_malformed,
                "tool_call_unknown_name": t.tool_call_unknown_name,
                "turns_with_text_fallback": t.turns_with_text_fallback,
                "first_text_fallback_iteration": t.first_text_fallback_iteration,
                "failure_category": t.failure_category,
                "failure_detail": t.failure_detail,
                "duration_s": t.duration_s,
            }
        )
    if orphans:
        distinct = sorted(set(orphans))
        warnings.warn(
            f"dropped {len(orphans)} task(s) whose run_id is absent from runs_df: "
            f"{distinct}",
            stacklevel=2,
        )
    df = _apply_schema(rows, _TASKS_SCHEMA)
    # started_at join above carries pandas Timestamps; _apply_schema's
    # to_datetime is a no-op pass-through that also normalises an empty frame.
    return df


def build_postconditions_df(
    tasks: list[TaskRecord],
) -> pd.DataFrame:
    """Construct ``postconditions_df`` (one row per postcondition evaluation).

    Tasks with an empty ``postconditions`` array contribute zero rows (never a
    None-filled placeholder row). Rows are ordered by (task_index, then the
    postcondition's appearance order) within each task, in task-record order.
    Pure.
    """
    rows: list[dict[str, Any]] = []
    for t in tasks:
        for pc in t.postconditions:
            rows.append(
                {
                    "run_id": t.run_id,
                    "task_id": t.task_id,
                    "postcondition_name": pc.name,
                    "passed": pc.passed,
                }
            )
    return _apply_schema(rows, _POSTCONDITIONS_SCHEMA)


def load_campaign(output_dir: str | Path) -> dict[str, pd.DataFrame]:
    """Load an output dir and build all three frames in one call.

    Returns ``{"runs": runs_df, "tasks": tasks_df, "postconditions":
    postconditions_df}``. ``campaign_name`` is the basename of ``output_dir``
    when a ``plan.jsonl`` is present there, else ``None``.
    """
    out = Path(output_dir)
    runs, tasks, plan = load_run_records(out)
    campaign_name = out.name if plan is not None else None
    runs_df = build_runs_df(runs, plan=plan, campaign_name=campaign_name)
    tasks_df = build_tasks_df(tasks, runs_df)
    postconditions_df = build_postconditions_df(tasks)
    return {"runs": runs_df, "tasks": tasks_df, "postconditions": postconditions_df}
