"""Layer-2 analysis: reproducibility, behavioral classification, capability vectors.

Layer 1 (:mod:`agora.observe.analysis`) turns campaign JSONL into three tidy
frames. Layer 2 consumes ``tasks_df`` / ``runs_df`` and answers the
characterization questions the raw counts can't on their own:

1. **Reproducibility** — with fixed seed + temperature, do a model's repeated
   runs of one task produce the *same trajectory*? :func:`reproducibility_by_cell`
   scores each (model, task) cell; :func:`model_metrics` rolls it up per model.
   This is a reliability axis orthogonal to pass rate.
2. **Behavioral classification** — the four classes observed in the axis-1 sweep
   (``structured-succeeds`` / ``structured-fragile`` / ``narrate-fallback`` /
   ``mixed-fails``) codified as a deterministic rule over the JSONL-derived
   metrics (:func:`classify`), so future characterization runs classify the same
   way without a human eyeballing the table.
3. **Capability vectors** — :func:`capability_vectors` emits the CSV schema
   (``campaign, model, strategy, axis, sub_target, raw_value, normalized_score,
   repeats, excluded_repeats, ci_low, ci_high``): one row per
   (model, strategy, sub_target). The ``campaign`` + ``strategy`` columns let
   axis-1 v1 (strategy null) and v2 (control + per-model treatment) rows coexist
   in one CSV.

Everything here is pure (same input → same output, no I/O, no globals). Nullable
pandas dtypes from Layer 1 are handled by coercing to float before arithmetic so
``pd.NA`` never leaks into a comparison.

Requires the ``analysis`` optional dependency group (pandas).
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

__all__ = [
    "AXIS_TOOL_CALL_FIDELITY",
    "TRAJECTORY_FIELDS",
    "capability_vectors",
    "classify",
    "model_metrics",
    "reproducibility_by_cell",
    "trajectory_signatures",
]

#: The axis label every axis-1 capability-vector row carries. Future axes
#: (instruction_adherence, code_body_correctness, …) append rows with their own
#: label to the same CSV — the row-per-sub_target design needs no column change.
AXIS_TOOL_CALL_FIDELITY = "tool_call_fidelity"

#: Task-record fields that together define a run's *trajectory* on a task. Two
#: repeats are "the same trajectory" iff they agree on every one of these. Chosen
#: to capture behaviour (what the model did) not wall-clock (duration_s is
#: excluded — it's environment noise, not a behavioural difference).
TRAJECTORY_FIELDS: tuple[str, ...] = (
    "status",
    "iterations",
    "tool_calls_total",
    "tool_calls_structured",
    "tool_calls_text_fallback",
    "tool_calls_malformed",
)

# ---- classification thresholds (module constants so the rule is inspectable) ----

#: text_fallback / total_calls at or above this ⇒ the model narrates instead of
#: emitting structured calls (``narrate-fallback``).
FALLBACK_DOMINANT_MIN = 0.75
#: Below this fallback share the model is "structured-dominant"; between the two
#: bounds it's a genuine mix of both channels (``mixed-*``).
STRUCTURED_DOMINANT_MAX = 0.25
#: Task pass fraction at or above this counts as "succeeds".
PASS_OK_MIN = 0.5
#: Per-model trajectory reproducibility at or above this counts as "reproducible"
#: (a structured-dominant, high-passing but flaky model is ``fragile``, not
#: ``succeeds``).
REPRODUCIBLE_MIN = 0.999


def _f(series: pd.Series) -> pd.Series:
    """Coerce a (possibly nullable-dtyped) numeric series to plain float64.

    Int64/boolean columns carry ``pd.NA``; comparing or dividing those raises
    "boolean value of NA is ambiguous". Float64 uses NaN, which propagates
    quietly through arithmetic — exactly what the aggregations below want.
    """
    return series.astype("float64")


# ------------------------------------------------------------------ reproducibility


def trajectory_signatures(tasks_df: pd.DataFrame) -> pd.Series:
    """Return one hashable trajectory tuple per task row (index-aligned).

    The tuple is ``TRAJECTORY_FIELDS`` in order. NA/NaN entries are normalised to
    ``None`` so two NA rows compare equal.
    """

    def _row(row: pd.Series) -> tuple:
        out: list[Any] = []
        for field in TRAJECTORY_FIELDS:
            val = row[field]
            if pd.isna(val):
                out.append(None)
            elif isinstance(val, float) and val.is_integer():
                out.append(int(val))
            else:
                out.append(val)
        return tuple(out)

    if tasks_df.empty:
        return pd.Series([], dtype="object")
    return tasks_df.apply(_row, axis=1)


def reproducibility_by_cell(tasks_df: pd.DataFrame) -> pd.DataFrame:
    """Per (model, task_id) cell: how consistent are the repeats' trajectories?

    Columns: ``model, task_id, n_repeats, n_distinct_trajectories,
    modal_share, reproducible``. ``modal_share`` is the fraction of repeats on
    the most common trajectory (1.0 = perfectly reproducible); ``reproducible``
    is ``n_distinct_trajectories == 1``.
    """
    cols = [
        "model",
        "task_id",
        "n_repeats",
        "n_distinct_trajectories",
        "modal_share",
        "reproducible",
    ]
    if tasks_df.empty:
        return pd.DataFrame(columns=cols)

    df = tasks_df.copy()
    df["_sig"] = trajectory_signatures(df)
    rows: list[dict[str, Any]] = []
    for (model, task_id), grp in df.groupby(["model", "task_id"], dropna=False):
        counts = grp["_sig"].value_counts()
        n = int(len(grp))
        rows.append(
            {
                "model": model,
                "task_id": task_id,
                "n_repeats": n,
                "n_distinct_trajectories": int(len(counts)),
                "modal_share": float(counts.iloc[0]) / n if n else float("nan"),
                "reproducible": bool(len(counts) == 1),
            }
        )
    return pd.DataFrame(rows, columns=cols)


# ------------------------------------------------------------------ per-run / per-model


def _per_run_metrics(tasks_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate task rows to one row per (model, campaign_run_id).

    Columns: model, campaign_run_id, total/structured/fallback/malformed (call
    sums), n_tasks, n_passed, and the per-run rates structured_rate /
    fallback_rate / malformed_rate (NaN when the run made zero calls) plus
    pass_rate and content_adaptation (1.0/0.0/NaN for the content_robustness
    task).
    """
    g = tasks_df.groupby(["model", "campaign_run_id"], dropna=False)
    agg = g.agg(
        total=("tool_calls_total", lambda s: _f(s).sum()),
        structured=("tool_calls_structured", lambda s: _f(s).sum()),
        fallback=("tool_calls_text_fallback", lambda s: _f(s).sum()),
        malformed=("tool_calls_malformed", lambda s: _f(s).sum()),
        n_tasks=("status", "size"),
        n_passed=("status", lambda s: (s == "passed").sum()),
    ).reset_index()

    total = agg["total"].replace(0, float("nan"))
    agg["structured_rate"] = agg["structured"] / total
    agg["fallback_rate"] = agg["fallback"] / total
    agg["malformed_rate"] = agg["malformed"] / total
    agg["pass_rate"] = agg["n_passed"] / agg["n_tasks"]

    # content-adaptation: pass/fail on the content_robustness task, per run.
    cr = tasks_df[tasks_df["task_id"] == "content_robustness"]
    ca = (
        cr.assign(_pass=(cr["status"] == "passed").astype("float64"))
        .groupby(["model", "campaign_run_id"], dropna=False)["_pass"]
        .max()
        .rename("content_adaptation")
        .reset_index()
    )
    agg = agg.merge(ca, on=["model", "campaign_run_id"], how="left")
    return agg


def _mean_ci(values: list[float], *, clip01: bool = True) -> dict[str, float]:
    """Mean + 95% normal-approx CI over the defined (non-NaN) values.

    ``n`` counts only defined values. n==0 ⇒ all NaN; n==1 ⇒ point value, NaN CI.
    Bounds are clipped to [0, 1] when ``clip01`` (rates); left unclipped for
    counts/iterations.
    """
    v = [float(x) for x in values if not pd.isna(x)]
    n = len(v)
    if n == 0:
        return {"mean": float("nan"), "lo": float("nan"), "hi": float("nan"), "n": 0}
    mean = sum(v) / n
    if n == 1:
        return {"mean": mean, "lo": float("nan"), "hi": float("nan"), "n": 1}
    var = sum((x - mean) ** 2 for x in v) / (n - 1)
    sem = math.sqrt(var) / math.sqrt(n)
    lo, hi = mean - 1.96 * sem, mean + 1.96 * sem
    if clip01:
        lo, hi = max(0.0, lo), min(1.0, hi)
    return {"mean": mean, "lo": lo, "hi": hi, "n": n}


def model_metrics(tasks_df: pd.DataFrame) -> pd.DataFrame:
    """Per-model rollup used by :func:`classify` and :func:`capability_vectors`.

    One row per model. Rate columns are the mean across the model's runs;
    ``*_lo`` / ``*_hi`` are 95% CI bounds; ``n_runs`` is the run count (the
    ``repeats`` value). ``first_fallback_p50`` is the median first-fallback
    iteration over task rows that fell back (NaN if the model never did).
    ``reproducibility`` is the mean of the per-cell ``reproducible`` flag.
    """
    rate_targets = [
        "structured_rate",
        "fallback_rate",
        "malformed_rate",
        "pass_rate",
        "content_adaptation",
    ]
    cols = (
        ["model", "n_runs", "first_fallback_p50", "reproducibility"]
        + rate_targets
        + [f"{t}_lo" for t in rate_targets]
        + [f"{t}_hi" for t in rate_targets]
    )
    if tasks_df.empty:
        return pd.DataFrame(columns=cols)

    per_run = _per_run_metrics(tasks_df)
    repro = reproducibility_by_cell(tasks_df)
    repro_by_model = (
        repro.assign(_r=repro["reproducible"].astype("float64"))
        .groupby("model")["_r"]
        .mean()
    )

    rows: list[dict[str, Any]] = []
    for model, grp in per_run.groupby("model", dropna=False):
        row: dict[str, Any] = {"model": model, "n_runs": int(len(grp))}
        for t in rate_targets:
            ci = _mean_ci(list(grp[t]))
            row[t] = ci["mean"]
            row[f"{t}_lo"] = ci["lo"]
            row[f"{t}_hi"] = ci["hi"]
        # first-fallback p50 over the model's task rows that fell back.
        ff = _f(tasks_df[tasks_df["model"] == model]["first_text_fallback_iteration"]).dropna()
        row["first_fallback_p50"] = float(ff.median()) if len(ff) else float("nan")
        row["reproducibility"] = float(repro_by_model.get(model, float("nan")))
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


# ------------------------------------------------------------------ classification


def classify(metrics_row: pd.Series | dict[str, Any]) -> str:
    """Assign one behavioural class from a :func:`model_metrics` row.

    Rule (evaluated top-down):

    - ``inert`` — the model emitted no tool calls at all (rates undefined).
    - ``narrate-fallback`` — fallback share ≥ ``FALLBACK_DOMINANT_MIN``.
    - ``mixed-fails`` / ``mixed-succeeds`` — meaningful mix of both channels
      (fallback share between the two bounds), split by ``PASS_OK_MIN``.
    - ``structured-succeeds`` — structured-dominant, passes ≥ ``PASS_OK_MIN``,
      and reproducible.
    - ``structured-fragile`` — structured-dominant but low-passing or flaky.
    """
    m = metrics_row
    struct = m["structured_rate"]
    fb = m["fallback_rate"]
    passr = m["pass_rate"]
    repro = m["reproducibility"]

    # No calls at all ⇒ both structured_rate and fallback_rate are NaN.
    if pd.isna(struct) and pd.isna(fb):
        return "inert"

    fb = 0.0 if pd.isna(fb) else float(fb)
    passr = 0.0 if pd.isna(passr) else float(passr)
    repro = 0.0 if pd.isna(repro) else float(repro)

    if fb >= FALLBACK_DOMINANT_MIN:
        return "narrate-fallback"
    if fb > STRUCTURED_DOMINANT_MAX:  # genuine mix of both channels
        return "mixed-succeeds" if passr >= PASS_OK_MIN else "mixed-fails"
    # structured-dominant
    if passr >= PASS_OK_MIN and repro >= REPRODUCIBLE_MIN:
        return "structured-succeeds"
    return "structured-fragile"


def classify_models(tasks_df: pd.DataFrame) -> pd.DataFrame:
    """Per-model behavioural class + the metrics that drove it."""
    mm = model_metrics(tasks_df)
    if mm.empty:
        return pd.DataFrame(
            columns=[
                "model",
                "behavioral_class",
                "structured_rate",
                "fallback_rate",
                "pass_rate",
                "reproducibility",
            ]
        )
    mm = mm.copy()
    mm["behavioral_class"] = mm.apply(classify, axis=1)
    return mm[
        [
            "model",
            "behavioral_class",
            "structured_rate",
            "fallback_rate",
            "pass_rate",
            "reproducibility",
        ]
    ]


# ------------------------------------------------------------------ capability vectors

#: (sub_target, metric-column-in-model_metrics, higher_is_better, normalize?)
#: ``normalize`` False ⇒ normalized_score is left null (direction/scale not yet
#: locked). Identity normalization (score == raw) is a *provisional* placeholder
#: for the higher-is-better 0–1 rates until the real normalization is locked.
_SUB_TARGETS: tuple[tuple[str, str, bool, bool], ...] = (
    ("structured_emission_rate", "structured_rate", True, True),
    ("text_fallback_rate", "fallback_rate", False, False),
    ("malformed_call_rate", "malformed_rate", False, False),
    ("content_adaptation_rate", "content_adaptation", True, True),
    ("pass_rate", "pass_rate", True, True),
    ("trajectory_reproducibility_rate", "reproducibility", True, True),
    ("first_fallback_iteration_p50", "first_fallback_p50", False, False),
)

_CAP_COLUMNS = (
    "campaign",
    "model",
    "strategy",
    "axis",
    "sub_target",
    "raw_value",
    "normalized_score",
    "repeats",
    "excluded_repeats",
    "ci_low",
    "ci_high",
)

#: The one sub_target whose value may be recomputed on a repeat subset (dropping
#: prewarm-contaminated runs); ``reproducibility_override`` targets it.
_REPRODUCIBILITY_SUB_TARGET = "trajectory_reproducibility_rate"


def capability_vectors(
    tasks_df: pd.DataFrame,
    runs_df: pd.DataFrame | None = None,
    *,
    axis: str = AXIS_TOOL_CALL_FIDELITY,
    campaign: str = "",
    reproducibility_override: dict[str, tuple[float | None, int]] | None = None,
) -> pd.DataFrame:
    """Emit the locked capability-vector CSV schema, one row per (model, strategy, sub_target).

    Columns: ``campaign, model, strategy, axis, sub_target, raw_value,
    normalized_score, repeats, excluded_repeats, ci_low, ci_high``. ``campaign``
    is the caller-supplied label (constant per invocation) that lets v1 and v2
    rows coexist in one CSV. ``strategy`` is the per-model prompting strategy
    (axis-1 v2); rows are keyed on ``(model, strategy, sub_target)`` so a model's
    control (strategy null) and treatment cells stay separate. When ``tasks_df``
    has no ``strategy`` column (v1), every row carries strategy null.
    ``model`` is the profile name when
    ``runs_df`` supplies a ``model → profile_name`` map, else the raw model
    string. ``repeats`` is the model's run count (the design repeat count, kept
    constant); ``excluded_repeats`` is how many of those were dropped from the
    reported value (0 unless overridden). ``normalized_score`` is identity for
    higher-is-better 0–1 rates (provisional) and null otherwise.

    ``reproducibility_override`` maps ``model string → (steady_rate,
    excluded_repeats)``. When a model is present, its
    ``trajectory_reproducibility_rate`` row reports ``steady_rate`` (the
    reproducibility recomputed after dropping the contaminated block-first run)
    with the given ``excluded_repeats``, instead of the all-repeats value. This
    keeps the campaign-layout concept of "block-first" out of the pure module —
    the caller decides which runs to exclude and passes the result as data.
    """
    if tasks_df.empty:
        return pd.DataFrame(columns=_CAP_COLUMNS)

    override = reproducibility_override or {}

    # model string → profile label (falls back to the raw model string).
    label: dict[str, str] = {}
    if runs_df is not None and not runs_df.empty:
        for _, r in runs_df[["model", "profile_name"]].drop_duplicates().iterrows():
            if pd.notna(r["profile_name"]):
                label[r["model"]] = r["profile_name"]

    # Key cells on strategy: a v2 model appears in both control (strategy null)
    # and treatment runs, and collapsing them would average two arms into one
    # cell. v1 tasks_df has no strategy column ⇒ one null group, unchanged.
    if "strategy" in tasks_df.columns:
        groups = list(tasks_df.groupby("strategy", dropna=False))
    else:
        groups = [(None, tasks_df)]

    rows: list[dict[str, Any]] = []
    for strat_val, sub in groups:
        strategy = None if pd.isna(strat_val) else str(strat_val)
        mm = model_metrics(sub)
        for _, m in mm.iterrows():
            model = m["model"]
            repeats = int(m["n_runs"])
            for sub_target, col, _higher_better, normalize in _SUB_TARGETS:
                raw = m[col]
                lo = m.get(f"{col}_lo", float("nan"))
                hi = m.get(f"{col}_hi", float("nan"))
                excluded = 0
                if sub_target == _REPRODUCIBILITY_SUB_TARGET and model in override:
                    raw, excluded = override[model]
                    lo = hi = float("nan")  # recomputed subset carries no CI
                norm = raw if (normalize and not pd.isna(raw)) else None
                rows.append(
                    {
                        "campaign": campaign,
                        "model": label.get(model, model),
                        "strategy": strategy,
                        "axis": axis,
                        "sub_target": sub_target,
                        "raw_value": None if pd.isna(raw) else float(raw),
                        "normalized_score": None if norm is None or pd.isna(norm) else float(norm),
                        "repeats": repeats,
                        "excluded_repeats": int(excluded),
                        "ci_low": None if pd.isna(lo) else float(lo),
                        "ci_high": None if pd.isna(hi) else float(hi),
                    }
                )
    return pd.DataFrame(rows, columns=_CAP_COLUMNS)
