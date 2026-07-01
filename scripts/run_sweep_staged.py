"""Staged orchestration wrapper around scripts/run_campaign.py.

Runs the axis-1 tool-call fidelity sweep in four checkpoints, pausing after
each so the user can review interim JSONL before committing more GPU time.
This is an ADDITIONAL entry point that composes run_campaign.py — it does not
replace it and adds no infrastructure. Every metric in the per-stage reports
comes from the Layer-1 analysis pipeline (``agora.observe.analysis``) plus
trivial inline pandas; no bespoke aggregation code lives here.

Usage:

    python scripts/run_sweep_staged.py --campaign <yaml> --stage {1|2|3|4a|4b|all}
                                       [--report-only] [--output-dir <dir>]

Stages (cumulative, in r001–r036 execution order):
    1  → r001            infrastructure validation
    2  → r001-r006       repeat agreement (Q3)
    3  → r001-r012       first cross-model comparison (Q1)
    4a → r001-r030       broad cross-model view (5 models)
    4b → r001-r036       full sweep (adds qwen3-30b)

The harness is idempotent: `--stage 3` after `--stage 2` completed just runs
r007-r012 (resume skips the rest). SIGINT propagates to run_campaign.py, which
owns graceful shutdown — this wrapper does not reimplement interrupt handling.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import traceback
import warnings
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
# Repo root on the path so `scripts.run_campaign` imports when this file is run
# directly (python scripts/run_sweep_staged.py), not just under pytest.
sys.path.insert(0, str(REPO_ROOT))

import agora.observe.analysis as analysis  # noqa: E402
from agora.plan.harness import force_utf8_stdio  # noqa: E402
from scripts.run_campaign import (  # noqa: E402
    Campaign,
    expand_plan,
    load_campaign,
    resume_filter,
    scan_done,
)

# ------------------------------------------------------------------ stage model

#: stage key → (cumulative run count, report filename, title). Rank is the
#: 1-based position, used to include report sections cumulatively.
STAGES: dict[str, dict[str, Any]] = {
    "1": {"count": 1, "report": "stage_1.md", "title": "Infrastructure validation", "rank": 1},
    "2": {"count": 6, "report": "stage_2.md", "title": "Repeat agreement (Q3)", "rank": 2},
    "3": {"count": 12, "report": "stage_3.md", "title": "First cross-model comparison (Q1)", "rank": 3},
    "4a": {"count": 30, "report": "stage_4a.md", "title": "Broad cross-model view", "rank": 4},
    "4b": {"count": 36, "report": "stage_4b.md", "title": "Full sweep (all six models)", "rank": 5},
}

#: The explicit continue-prompt printed at the end of each stage report.
PROMPTS: dict[str, str] = {
    "1": "Review. Continue to Stage 2?\n    python scripts/run_sweep_staged.py --campaign {campaign} --stage 2",
    "2": "Repeat agreement acceptable? Continue to Stage 3?\n    python scripts/run_sweep_staged.py --campaign {campaign} --stage 3",
    "3": "Continue to Stage 4a (batch of 3 models)?\n    python scripts/run_sweep_staged.py --campaign {campaign} --stage 4a",
    "4a": "Continue to Stage 4b (final: add qwen3-30b)?\n    python scripts/run_sweep_staged.py --campaign {campaign} --stage 4b",
    "4b": "Sweep complete. Layer 2 analysis is the next step.",
}

#: When --stage 4 (unqualified) or --stage all is passed, run these in order.
_STAGE_ALIASES: dict[str, list[str]] = {
    "4": ["4a", "4b"],
    "all": ["1", "2", "3", "4a", "4b"],
}


def stage_target_ids(campaign: Campaign, stage: str) -> list[str]:
    """The campaign run ids this stage targets: the first N in declared order."""
    count = STAGES[stage]["count"]
    plan = expand_plan(campaign)
    return [run["id"] for run in plan[:count]]


def resolve_output_dir(campaign: Campaign, override: str | Path | None) -> Path:
    """Report/resume target: the --output-dir override, else the campaign's."""
    return Path(override) if override is not None else Path(campaign.defaults.output_dir)


# ------------------------------------------------------------------ report helpers


def _traced(label: str, expr: str, value: Any) -> str:
    """A report line whose number is traceable to the pandas expression."""
    return f"- {label}: `{expr}` → **{value}**"


def _numeric_fields() -> list[str]:
    # Behavioral fields only — deterministic at temp 0 / seed 42, so any
    # divergence across repeats or between arms is a real signal. duration_s is
    # deliberately excluded: wall-clock timing is inherently non-deterministic
    # and would flood the agreement/divergence checks with false positives.
    return [
        "iterations",
        "loopback_count",
        "tool_calls_total",
        "tool_calls_structured",
        "tool_calls_text_fallback",
        "tool_calls_malformed",
        "tool_call_unknown_name",
        "turns_with_text_fallback",
        "first_text_fallback_iteration",
    ]


def _fmt(value: Any) -> str:
    """Compact float formatting for report cells."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f != f:  # NaN
        return "NaN"
    return f"{f:.3g}"


# ------------------------------------------------------------------ report sections
# Each returns a list of markdown lines. All numbers are inline pandas over the
# Layer-1 frames — the expression is shown alongside every value.


def _section_header(campaign: Campaign, stage: str, frames: dict, target_ids: list[str]) -> list[str]:
    runs = frames["runs"]
    present_target = int(runs["campaign_run_id"].isin(target_ids).sum())
    lines = [
        f"# {campaign.name} — Stage {stage}: {STAGES[stage]['title']}",
        "",
        f"- Stage target: **{len(target_ids)}** run(s) (`{target_ids[0]}`..`{target_ids[-1]}`)",
        _traced(
            "Target runs complete",
            "runs['campaign_run_id'].isin(target_ids).sum()",
            f"{present_target} of {len(target_ids)}",
        ),
        _traced("Runs present in dir", "len(runs)", len(runs)),
    ]
    if len(runs) and runs["campaign_run_id"].isna().all():
        lines.append(
            "- NOTE: no plan.jsonl found — this is a standalone/degraded dir "
            "(campaign_run_id is null; single-run layout)."
        )
    return lines


def _section_schema(output_dir: Path) -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        runs, tasks, _plan = analysis.load_run_records(output_dir)
    msgs = [str(w.message) for w in caught]
    lines = [
        "",
        "## Schema validation",
        _traced("RunRecords parsed", "len(load_run_records()[0])", len(runs)),
        _traced("TaskRecords parsed", "len(load_run_records()[1])", len(tasks)),
        _traced("Validation warnings", "len(caught_warnings)", len(msgs)),
    ]
    for m in msgs:
        lines.append(f"  - warning: {m}")
    return lines


def _section_invariant(frames: dict) -> list[str]:
    t = frames["tasks"]
    if len(t) == 0:
        return ["", "## Tool-call invariant", "- (no task rows)"]
    holds = bool(
        (t["tool_calls_structured"] + t["tool_calls_text_fallback"] == t["tool_calls_total"]).all()
    )
    return [
        "",
        "## Tool-call invariant",
        _traced(
            "structured + text_fallback == total (all rows)",
            "(t['tool_calls_structured'] + t['tool_calls_text_fallback'] == t['tool_calls_total']).all()",
            holds,
        ),
    ]


def _section_field_population(frames: dict) -> list[str]:
    t = frames["tasks"]
    if len(t) == 0:
        return ["", "## Field population", "- (no task rows)"]
    ftf = t["first_text_fallback_iteration"].notna().mean()
    failed = t[t["status"] == "failed"]
    fc = failed["failure_category"].notna().mean() if len(failed) else float("nan")
    return [
        "",
        "## Field population (observations, not judgements)",
        _traced(
            "rows with first_text_fallback_iteration populated",
            "t['first_text_fallback_iteration'].notna().mean()",
            _fmt(ftf),
        ),
        _traced(
            "failed rows with failure_category populated",
            "t[t.status=='failed']['failure_category'].notna().mean()",
            _fmt(fc),
        ),
    ]


def _section_runtime(frames: dict) -> list[str]:
    runs, t = frames["runs"], frames["tasks"]
    lines = ["", "## Runtime"]
    if len(runs):
        lines.append(_traced("total run duration_s (sum)", "runs['duration_s'].sum()", _fmt(runs["duration_s"].sum())))
    if len(t):
        d = t["duration_s"]
        lines.append(
            _traced(
                "task duration_s min/mean/max",
                "t['duration_s'].agg(['min','mean','max'])",
                f"{_fmt(d.min())} / {_fmt(d.mean())} / {_fmt(d.max())}",
            )
        )
    return lines


def _section_passfail(frames: dict) -> list[str]:
    t = frames["tasks"]
    lines = ["", "## Pass/fail summary"]
    if len(t) == 0:
        return lines + ["- (no task rows)"]
    counts = t["status"].value_counts().to_dict()
    lines.append(_traced("task status counts", "t['status'].value_counts()", counts))
    return lines


def _section_repeat_agreement(frames: dict) -> list[str]:
    t = frames["tasks"]
    lines = ["", "## Repeat agreement (across the 3 repeats per (model, arm, task))"]
    if len(t) == 0 or t["repeat"].isna().all():
        return lines + ["- (no repeat data — no plan.jsonl / campaign_run_id null)"]
    fields = [f for f in _numeric_fields() if f in t.columns]
    grouped = t.groupby(["model", "scaffolding", "task_id"], dropna=False)
    lines.append(
        "- grouping: `t.groupby(['model','scaffolding','task_id'])`; per field, the "
        "repeats' values via `g[field].tolist()`"
    )
    any_divergent = False
    for (model, scaffolding, task_id), g in grouped:
        divergent = [f for f in fields if g[f].nunique(dropna=False) > 1]
        if divergent:
            any_divergent = True
            detail = "; ".join(f"{f}={g[f].tolist()}" for f in divergent)
            lines.append(f"  - **DIFFERS** {model}/{scaffolding}/{task_id}: {detail}")
    if not any_divergent:
        lines.append("- all numeric fields identical across repeats for every group.")
    # Postcondition agreement.
    pc = frames["postconditions"]
    if len(pc):
        lines.append(
            _traced(
                "postcondition (name,passed) unique tuples",
                "pc.groupby(['task_id','postcondition_name'])['passed'].nunique().max()",
                int(pc.groupby(["task_id", "postcondition_name"])["passed"].nunique().max()),
            )
        )
    return lines


def _section_lean_vs_rich(frames: dict) -> list[str]:
    t = frames["tasks"]
    lines = ["", "## Lean vs rich (must be identical — Phase B deferral)"]
    if len(t) == 0 or t["scaffolding"].nunique() < 2:
        return lines + ["- (only one arm present so far — nothing to compare)"]
    fields = [f for f in _numeric_fields() if f in t.columns]
    # Compare lean vs rich means per (model, task_id).
    piv = t.groupby(["model", "task_id", "scaffolding"], dropna=False)[fields].mean()
    lean = piv.xs("lean", level="scaffolding", drop_level=True)
    rich = piv.xs("rich", level="scaffolding", drop_level=True)
    common = lean.index.intersection(rich.index)
    if len(common) == 0:
        return lines + ["- (no overlapping (model, task) between arms yet)"]
    diff = (lean.loc[common] - rich.loc[common]).abs()
    # NA-safe max: nullable columns (e.g. first_text_fallback_iteration) carry
    # <NA> where a field doesn't apply, and NA - NA = NA. pandas' skipna max
    # handles that; numpy's .to_numpy().max() raised "boolean value of NA is
    # ambiguous". All-NA (both arms absent) → treat as no difference.
    overall = diff.max(skipna=True).max(skipna=True)
    max_abs = 0.0 if pd.isna(overall) else float(overall)
    identical = max_abs == 0.0
    lines.append(
        _traced(
            "max |lean_mean - rich_mean| across fields",
            "(lean_means - rich_means).abs().max().max()",
            _fmt(max_abs),
        )
    )
    if identical:
        lines.append("- lean and rich are identical, as expected for v1.")
    else:
        lines.append("- **FLAG**: lean and rich diverge — this is a bug in v1 (arms should be identical).")
    return lines


def _section_cross_model(frames: dict) -> list[str]:
    t = frames["tasks"]
    lines = ["", "## Cross-model comparison (Q1: does the probe distinguish models?)"]
    if len(t) == 0 or t["model"].isna().all():
        return lines + ["- (no model data)"]
    fields = [f for f in _numeric_fields() if f in t.columns]
    per_model = t.groupby("model", dropna=False)[fields].mean()
    lines.append("- per-model means: `t.groupby('model')[fields].mean()`")
    for model, row in per_model.iterrows():
        cells = ", ".join(f"{f}={_fmt(row[f])}" for f in ("tool_calls_text_fallback", "iterations", "tool_calls_total"))
        lines.append(f"  - {model}: {cells}")
    # Discrimination: between-model variance vs within-(model,repeat) variance.
    if t["model"].nunique() >= 2:
        between = float(per_model["tool_calls_total"].var(ddof=0))
        within = float(
            t.groupby(["model", "task_id"], dropna=False)["tool_calls_total"].var(ddof=0).mean()
        )
        lines.append(
            _traced(
                "discrimination: between-model var vs within-group var (tool_calls_total)",
                "per_model_means.var() ; t.groupby(['model','task_id']).var().mean()",
                f"{_fmt(between)} vs {_fmt(within)}",
            )
        )
    return lines


def _section_cross_model_table(frames: dict) -> list[str]:
    t = frames["tasks"]
    lines = ["", "## Cross-model summary table"]
    if len(t) == 0 or t["model"].isna().all():
        return lines + ["- (no model data)"]
    lines.append("- `t.groupby('model').agg(...)` — one row per model:")
    lines.append("")
    lines.append("| model | text_fallback mean | first_ftf populated frac | pass frac | tool_calls_total mean |")
    lines.append("|---|---|---|---|---|")
    g = t.groupby("model", dropna=False)
    tf = g["tool_calls_text_fallback"].mean()
    ftf = g["first_text_fallback_iteration"].apply(lambda s: s.notna().mean())
    passfrac = g["status"].apply(lambda s: (s == "passed").mean())
    total = g["tool_calls_total"].mean()
    for model in tf.index:
        lines.append(
            f"| {model} | {_fmt(tf[model])} | {_fmt(ftf[model])} | "
            f"{_fmt(passfrac[model])} | {_fmt(total[model])} |"
        )
    return lines


def _section_qwen3_think(frames: dict) -> list[str]:
    t = frames["tasks"]
    lines = ["", "## qwen3-30b <think>-block handling"]
    q3 = t[t["model_family"] == "qwen-thinking"] if "model_family" in t.columns else t.iloc[0:0]
    if len(q3) == 0:
        return lines + ["- (no qwen-thinking rows present)"]
    lines.append(
        "- NOTE: `content_len` is not a schema-v1 field, so <think> leakage can't be "
        "inspected directly. The observable proxy is the text-fallback signal: if a "
        "leaked <think> block confused the tool-call parser it would inflate these."
    )
    lines.append(
        _traced(
            "qwen3-30b tool_calls_text_fallback sum",
            "t[t.model_family=='qwen-thinking']['tool_calls_text_fallback'].sum()",
            int(q3["tool_calls_text_fallback"].sum()),
        )
    )
    lines.append(
        _traced(
            "qwen3-30b turns_with_text_fallback sum",
            "t[t.model_family=='qwen-thinking']['turns_with_text_fallback'].sum()",
            int(q3["turns_with_text_fallback"].sum()),
        )
    )
    return lines


def _section_prompt(campaign: Campaign, stage: str, campaign_path: str) -> list[str]:
    return ["", "## Next", "", PROMPTS[stage].format(campaign=campaign_path)]


def generate_report(
    stage: str,
    output_dir: str | Path,
    campaign: Campaign,
    campaign_path: str,
) -> str:
    """Build the stage's markdown report from Layer-1 frames. Never hides errors.

    On any Layer-1 failure (partial JSONL, schema error) the report states
    ``Layer 1 raised:`` with the traceback rather than silently omitting data.
    """
    target_ids = stage_target_ids(campaign, stage)
    rank = STAGES[stage]["rank"]
    out = Path(output_dir)
    try:
        frames = analysis.load_campaign(out)
    except Exception:  # noqa: BLE001 — surface, never hide (report-under-fatigue)
        tb = traceback.format_exc()
        return (
            f"# {campaign.name} — Stage {stage}: {STAGES[stage]['title']}\n\n"
            f"## Layer 1 raised\n\n```\n{tb}\n```\n"
        )

    # Assemble sections. A bug in any one section surfaces its traceback in the
    # report (never hides it, never loses the whole report) — same principle as
    # the Layer-1 guard above.
    sections = [
        lambda: _section_header(campaign, stage, frames, target_ids),
        lambda: _section_schema(out),
        lambda: _section_invariant(frames),
        lambda: _section_field_population(frames),
        lambda: _section_runtime(frames),
        lambda: _section_passfail(frames),
    ]
    if rank >= 2:
        sections += [lambda: _section_repeat_agreement(frames),
                     lambda: _section_lean_vs_rich(frames)]
    if rank >= 3:
        sections.append(lambda: _section_cross_model(frames))
    if rank >= 4:
        sections.append(lambda: _section_cross_model_table(frames))
    if rank >= 5:
        sections.append(lambda: _section_qwen3_think(frames))

    lines: list[str] = []
    for section in sections:
        try:
            lines += section()
        except Exception:  # noqa: BLE001 — surface the section failure in-report
            lines += ["", "## Report section raised", "", "```", traceback.format_exc(), "```"]
    lines += _section_prompt(campaign, stage, campaign_path)
    return "\n".join(lines) + "\n"


def write_report(stage: str, output_dir: str | Path, markdown: str) -> Path:
    """Write the report to ``<output_dir>/reports/stage_<N>.md`` and return the path."""
    reports_dir = Path(output_dir) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / STAGES[stage]["report"]
    path.write_text(markdown, encoding="utf-8")
    return path


# ------------------------------------------------------------------ execution


def _write_stage_campaign(
    campaign_path: str, target_ids: list[str], output_dir: Path, dest: Path
) -> Path:
    """Write a stage-scoped copy of the campaign (runs sliced to target_ids).

    Preserves defaults but pins output_dir to the shared sweep dir so per-run
    subdirs land together and resume works across stages. run_campaign runs
    this subset in one process → the eviction-across-runs optimisation holds.
    """
    raw = yaml.safe_load(Path(campaign_path).read_text(encoding="utf-8"))
    target = set(target_ids)
    raw["runs"] = [r for r in raw.get("runs", []) if r.get("id") in target]
    raw.setdefault("defaults", {})["output_dir"] = str(output_dir)
    dest.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return dest


def _run_summary_line(output_dir: Path, run_id: str) -> str:
    """One-line summary for a completed run, read from its run.jsonl."""
    run_jsonl = output_dir / run_id / "run.jsonl"
    if not run_jsonl.is_file():
        return f"  {run_id}: (no run.jsonl — not executed)"
    try:
        rec = json.loads(run_jsonl.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, json.JSONDecodeError, IndexError) as exc:
        return f"  {run_id}: (unreadable run.jsonl: {exc})"
    mark = "OK" if rec.get("success") else "FAIL"
    return (
        f"  [{mark}] {run_id} {rec.get('profile', {}).get('model', '?')}: "
        f"passed={rec.get('tasks_passed')} failed={rec.get('tasks_failed')} "
        f"duration={_fmt(rec.get('duration_s'))}s"
    )


def execute_stage(
    stage: str,
    campaign_path: str,
    campaign: Campaign,
    output_dir: Path,
) -> int:
    """Run this stage's pending runs via run_campaign.py; return its exit code.

    Prints a start-of-stage banner + the pending set, invokes run_campaign as a
    subprocess (its stdout streams per-run start lines and SIGINT handling), then
    prints a per-run summary read back from JSONL.
    """
    target_ids = stage_target_ids(campaign, stage)
    plan = expand_plan(campaign)
    done = scan_done(output_dir) if campaign.defaults.resume else set()
    pending_all = {r["id"] for r in resume_filter(plan, done)}
    pending_here = [rid for rid in target_ids if rid in pending_all]

    print("=" * 72)
    print(f"[sweep] Stage {stage}: {STAGES[stage]['title']}")
    print(f"[sweep] target: {target_ids[0]}..{target_ids[-1]} ({len(target_ids)} runs)")
    print(f"[sweep] already done: {len(target_ids) - len(pending_here)}; pending now: {pending_here}")
    print("=" * 72)

    if not pending_here:
        print("[sweep] nothing pending for this stage; skipping execution.")
        return 0

    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stage_campaign = _write_stage_campaign(
        campaign_path, target_ids, output_dir, reports_dir / f".stage_{stage}_campaign.yaml"
    )
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "run_campaign.py"), str(stage_campaign)]
    # Inherit stdout/stderr so run_campaign's per-run lines + SIGINT flow through.
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT))

    print("\n[sweep] per-run results this stage:")
    for rid in target_ids:
        print(_run_summary_line(output_dir, rid))
    return proc.returncode


def _stage_has_failure(output_dir: Path, target_ids: list[str]) -> bool:
    """True if any target run recorded success: false."""
    for rid in target_ids:
        run_jsonl = output_dir / rid / "run.jsonl"
        if not run_jsonl.is_file():
            continue
        try:
            rec = json.loads(run_jsonl.read_text(encoding="utf-8").splitlines()[0])
        except (OSError, json.JSONDecodeError, IndexError):
            continue
        if rec.get("success") is False:
            return True
    return False


def _run_one_stage(
    stage: str,
    campaign_path: str,
    campaign: Campaign,
    output_dir: Path,
    *,
    report_only: bool,
) -> int:
    exit_code = 0
    if not report_only:
        rc = execute_stage(stage, campaign_path, campaign, output_dir)
        if rc != 0:
            exit_code = 1
    markdown = generate_report(stage, output_dir, campaign, campaign_path)
    path = write_report(stage, output_dir, markdown)
    print("\n" + markdown)
    print(f"[sweep] report written → {path}")
    if _stage_has_failure(output_dir, stage_target_ids(campaign, stage)):
        exit_code = 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    # Reports contain non-ASCII (→); Windows stdout is cp1252 by default. Done
    # here (not at import) so importing the module for tests has no stdio side
    # effect. Under pytest's utf-8 capture this is a no-op.
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="Staged axis-1 sweep runner + reports.")
    parser.add_argument("--campaign", required=True, help="Campaign YAML path.")
    parser.add_argument(
        "--stage", required=True, choices=["1", "2", "3", "4", "4a", "4b", "all"],
        help="Stage to run/report (4 = 4a then 4b; all = every stage in order).",
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="Regenerate the report(s) from existing JSONL; run nothing.",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Override the campaign's output_dir (report/resume target).",
    )
    args = parser.parse_args(argv)

    campaign = load_campaign(args.campaign)
    output_dir = resolve_output_dir(campaign, args.output_dir)
    stages = _STAGE_ALIASES.get(args.stage, [args.stage])

    exit_code = 0
    for stage in stages:
        rc = _run_one_stage(
            stage, args.campaign, campaign, output_dir, report_only=args.report_only
        )
        if rc != 0:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
