"""Layer-2 analysis driver for the axis-1 tool-call-fidelity campaign.

Consumes the validated Layer-1 frames (:mod:`agora.observe.analysis`) plus the
per-run raw logs and produces two artifacts under ``<campaign>/reports/``:

- ``layer2_findings.md`` — the five focused analyses:
    1. reproducibility as a first-class axis (trajectory divergence per model),
    2. behavioral classification (codified rule → per-model class),
    3. the qwen3 malformed-call dig (shape of the malformation, from run.log),
    4. the first-after-load cross-check (instruct r013 vs qwen3 r031 — shared
       prewarm-context mechanism, from snapshot_pre.txt),
    5. the capability-vector table (also written as CSV).
- ``capability_vectors.csv`` — the locked schema
  (``model, axis, sub_target, raw_value, normalized_score, repeats, ci_low,
  ci_high``).

Usage:  python scripts/analyze_layer2.py [--campaign runs_out/axis-1-tool-call-fidelity]

Pure analysis — reads the campaign dir, writes the two report files, touches no
model. The reusable scoring lives in :mod:`agora.observe.layer2`; the raw-log
parsing here is campaign-layout-specific and stays in the script.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

from agora.observe.analysis import load_campaign
from agora.observe.layer2 import (
    capability_vectors,
    classify_models,
    model_metrics,
    reproducibility_by_cell,
)

DEFAULT_CAMPAIGN = "runs_out/axis-1-tool-call-fidelity"

# run.log line: "... tool call: task=T turn=N name=X args={...} result=ERROR: tool X raised: MSG"
_TOOL_CALL_RE = re.compile(
    r"tool call: task=(?P<task>\S+) turn=(?P<turn>\d+) name=(?P<name>\w+) "
    r"args=(?P<args>\{.*?\}) result=(?P<result>.*)$"
)
_ARG_KEYS_RE = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)':")


# ------------------------------------------------------------------ raw-log digs


def parse_malformed_calls(run_dir: Path) -> list[dict[str, str]]:
    """Extract malformed tool calls (result starts ``ERROR: tool ``) from run.log.

    Returns dicts: task, tool, arg_keys (sorted tuple as a string), error tail.
    Characterizes the *shape* of the malformation the JSONL only counts.
    """
    log = run_dir / "run.log"
    if not log.is_file():
        return []
    out: list[dict[str, str]] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _TOOL_CALL_RE.search(line)
        if not m or not m.group("result").startswith("ERROR: tool "):
            continue
        keys = tuple(sorted(set(_ARG_KEYS_RE.findall(m.group("args")))))
        err = m.group("result").removeprefix("ERROR: tool ").strip()
        # collapse "<tool> raised: <msg>" → the raised message.
        err_tail = err.split("raised:", 1)[1].strip() if "raised:" in err else err
        out.append(
            {
                "task": m.group("task"),
                "tool": m.group("name"),
                "arg_keys": ", ".join(keys),
                "error": err_tail[:80],
            }
        )
    return out


def parse_load_context(run_dir: Path) -> int | None:
    """Read the loaded model's CONTEXT (num_ctx) from snapshot_pre.txt.

    The ``ollama ps`` table row lists ``… <PROCESSOR> GPU  <CONTEXT>  <UNTIL>``;
    we take the integer immediately after the ``GPU`` token. None if absent.
    """
    snap = run_dir / "snapshot_pre.txt"
    if not snap.is_file():
        return None
    for line in snap.read_text(encoding="utf-8", errors="replace").splitlines():
        if " GPU " not in line:
            continue
        toks = line.split()
        if "GPU" in toks:
            i = toks.index("GPU")
            if i + 1 < len(toks) and toks[i + 1].isdigit():
                return int(toks[i + 1])
    return None


# ------------------------------------------------------------------ report helpers


def _fmt(x: object, nd: int = 3) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _profile_label(runs_df: pd.DataFrame) -> dict[str, str]:
    return {
        r["model"]: r["profile_name"]
        for _, r in runs_df[["model", "profile_name"]].drop_duplicates().iterrows()
        if pd.notna(r["profile_name"])
    }


def _first_of_block(runs_df: pd.DataFrame) -> dict[str, str]:
    """model → its earliest campaign_run_id (the block's first, prewarm-loaded run)."""
    ordered = runs_df.sort_values("campaign_run_id")
    return {m: g["campaign_run_id"].iloc[0] for m, g in ordered.groupby("model")}


def _steady_tasks(tasks_df: pd.DataFrame, runs_df: pd.DataFrame) -> pd.DataFrame:
    """tasks_df minus each model's prewarm-contaminated block-first run."""
    fob = _first_of_block(runs_df)
    return tasks_df[
        ~tasks_df.apply(lambda r: fob.get(r["model"]) == r["campaign_run_id"], axis=1)
    ]


def reproducibility_override(
    tasks_df: pd.DataFrame, runs_df: pd.DataFrame
) -> dict[str, tuple[float, int]]:
    """model → (steady_rate, excluded_repeats) for models that show non-determinism.

    A fully-reproducible model (all-6 rate == 1.0) needs no exclusion → absent
    from the map (``excluded_repeats`` stays 0, all-6 value reported). A model
    with any divergence gets its 1 block-first run dropped and its steady-state
    reproducibility reported instead.
    """
    steady_cell = reproducibility_by_cell(_steady_tasks(tasks_df, runs_df))
    steady_by_model = (
        steady_cell.assign(_r=steady_cell["reproducible"].astype("float64"))
        .groupby("model")["_r"]
        .mean()
    )
    override: dict[str, tuple[float, int]] = {}
    for _, m in model_metrics(tasks_df).iterrows():
        all6 = m["reproducibility"]
        if pd.notna(all6) and all6 < 1.0:
            override[m["model"]] = (float(steady_by_model.get(m["model"], all6)), 1)
    return override


def build_report(campaign_dir: Path) -> str:
    frames = load_campaign(campaign_dir)
    runs_df, tasks_df = frames["runs"], frames["tasks"]
    label = _profile_label(runs_df)

    def name(model: str) -> str:
        return label.get(model, model)

    lines: list[str] = []
    w = lines.append
    w("# axis-1 tool-call-fidelity — Layer 2 findings\n")
    w(f"- campaign: `{campaign_dir}`")
    w(f"- runs: **{len(runs_df)}**, tasks: **{len(tasks_df)}**, "
      f"models: **{runs_df['model'].nunique()}**\n")

    # ---- 1. reproducibility --------------------------------------------------
    w("## 1. Reproducibility (trajectory divergence per model)\n")
    w("A (model, task) cell is *reproducible* when all its repeats share one "
      "trajectory over `status, iterations, tool_calls_{total,structured,"
      "text_fallback,malformed}`. `modal_share` = fraction on the most common "
      "trajectory.\n")
    repro = reproducibility_by_cell(tasks_df)
    fob = _first_of_block(runs_df)
    # steady-state: drop each model's prewarm-contaminated first-of-block run.
    repro_steady = reproducibility_by_cell(_steady_tasks(tasks_df, runs_df)).set_index(
        ["model", "task_id"]
    )
    w("| model | task | repeats | distinct | modal_share | reproducible | steady (excl. 1st-of-block) |")
    w("|---|---|---|---|---|---|---|")
    for _, r in repro.sort_values(["model", "task_id"]).iterrows():
        key = (r["model"], r["task_id"])
        st = repro_steady.loc[key] if key in repro_steady.index else None
        st_txt = "—" if st is None else ("yes" if bool(st["reproducible"]) else
                                         f"no ({int(st['n_distinct_trajectories'])})")
        w(f"| {name(r['model'])} | {r['task_id']} | {int(r['n_repeats'])} | "
          f"{int(r['n_distinct_trajectories'])} | {_fmt(r['modal_share'],2)} | "
          f"{'yes' if r['reproducible'] else 'no'} | {st_txt} |")
    mm = model_metrics(tasks_df)
    w("\nPer-model reproducibility score (mean of the cell flags, all repeats):\n")
    w("| model | reproducibility | pass_rate |")
    w("|---|---|---|")
    for _, m in mm.sort_values("reproducibility", ascending=False).iterrows():
        w(f"| {name(m['model'])} | {_fmt(m['reproducibility'],2)} | {_fmt(m['pass_rate'],2)} |")

    # ---- 2. behavioral classification ---------------------------------------
    w("\n## 2. Behavioral classification (codified rule)\n")
    w("Rule (top-down): `fallback_share ≥ 0.75` → **narrate-fallback**; "
      "`0.25 < fallback_share < 0.75` → **mixed-{fails,succeeds}** (by pass ≥ 0.5); "
      "structured-dominant with pass ≥ 0.5 **and** reproducible → "
      "**structured-succeeds**; else **structured-fragile**; no calls → **inert**.\n")
    cls = classify_models(tasks_df)
    w("| model | class | structured_rate | fallback_rate | pass_rate | reproducibility |")
    w("|---|---|---|---|---|---|")
    for _, c in cls.iterrows():
        w(f"| {name(c['model'])} | **{c['behavioral_class']}** | "
          f"{_fmt(c['structured_rate'],2)} | {_fmt(c['fallback_rate'],2)} | "
          f"{_fmt(c['pass_rate'],2)} | {_fmt(c['reproducibility'],2)} |")

    # ---- 3. malformed-call dig (qwen3) --------------------------------------
    w("\n## 3. Malformed-call dig\n")
    all_mal: list[dict[str, str]] = []
    for sub in sorted(p for p in campaign_dir.iterdir() if p.is_dir() and p.name != "reports"):
        for rec in parse_malformed_calls(sub):
            rec["run"] = sub.name
            all_mal.append(rec)
    if not all_mal:
        w("No malformed tool calls found in any run.log.\n")
    else:
        mal_df = pd.DataFrame(all_mal)
        w(f"Total malformed calls across the campaign: **{len(mal_df)}** "
          f"(runs: {', '.join(sorted(mal_df['run'].unique()))}).\n")
        w("Malformation shape (tool + arg keys the model sent + the raised error), counted:\n")
        w("| tool | arg_keys sent | error | count |")
        w("|---|---|---|---|")
        shape = mal_df.groupby(["tool", "arg_keys", "error"]).size().reset_index(name="n")
        for _, s in shape.sort_values("n", ascending=False).iterrows():
            w(f"| `{s['tool']}` | `{{{s['arg_keys']}}}` | `{s['error']}` | {int(s['n'])} |")

    # ---- 4. first-after-load cross-check ------------------------------------
    w("\n## 4. First-after-load cross-check (prewarm context)\n")
    w("Loaded `CONTEXT` (num_ctx) read from each run's `snapshot_pre.txt`. The "
      "campaign pins `num_ctx=8192`; the prewarm step loads the model at its "
      "*default* context, contaminating each block's first run. (`—` = the "
      "pre-run `ollama ps` probe timed out, no data. A non-first run showing "
      "32768 corresponds to a prewarm triggered by a manual re-run/resume — the "
      "same mechanism, not a block boundary.)\n")
    ctx_rows: list[dict[str, object]] = []
    for _, r in runs_df.sort_values("campaign_run_id").iterrows():
        rd = campaign_dir / r["campaign_run_id"]
        ctx_rows.append(
            {
                "run": r["campaign_run_id"],
                "model": name(r["model"]),
                "first_of_block": fob.get(r["model"]) == r["campaign_run_id"],
                "loaded_ctx": parse_load_context(rd),
                "pinned_ctx": int(r["num_ctx"]) if pd.notna(r["num_ctx"]) else None,
                "passed": int(r["tasks_passed"]) if pd.notna(r["tasks_passed"]) else None,
            }
        )
    ctx_df = pd.DataFrame(ctx_rows)
    firsts = ctx_df[ctx_df["first_of_block"]]
    steady = ctx_df[~ctx_df["first_of_block"]]
    w(f"- first-of-block runs ({', '.join(firsts['run'])}): "
      f"loaded_ctx = {sorted(set(firsts['loaded_ctx'].dropna()))} "
      f"(pinned {sorted(set(firsts['pinned_ctx'].dropna()))})")
    w(f"- steady-state runs: loaded_ctx = {sorted(set(steady['loaded_ctx'].dropna()))}\n")
    contaminated = firsts[firsts["loaded_ctx"] != firsts["pinned_ctx"]]
    w(f"**{len(contaminated)} of {len(firsts)} block-first runs ran at the wrong "
      f"(default) context** — a prewarm bug affecting every model block, not a "
      f"per-model quirk. This is the shared mechanism behind the instruct-r013 "
      f"and qwen3-r031 first-run anomalies.\n")
    w("| run | model | first-of-block | loaded_ctx | pinned_ctx | passed |")
    w("|---|---|---|---|---|---|")
    for _, r in ctx_df.iterrows():
        w(f"| {r['run']} | {r['model']} | {'★' if r['first_of_block'] else ''} | "
          f"{_fmt(r['loaded_ctx'])} | {_fmt(r['pinned_ctx'])} | {_fmt(r['passed'])} |")

    # ---- 5. capability vectors ----------------------------------------------
    w("\n## 5. Capability vectors\n")
    override = reproducibility_override(tasks_df, runs_df)
    cv = capability_vectors(tasks_df, runs_df, reproducibility_override=override)
    w("Locked schema `model, axis, sub_target, raw_value, normalized_score, "
      "repeats, excluded_repeats, ci_low, ci_high`. `repeats=6` (lean+rich "
      "combine in v1); `excluded_repeats` counts runs dropped from the reported "
      "value — nonzero only where a model's `trajectory_reproducibility_rate` is "
      "recomputed after excluding its prewarm-contaminated block-first run. "
      "`normalized_score` is provisional identity for higher-is-better 0–1 rates, "
      "null where scale/direction isn't locked. Written to "
      "`reports/capability_vectors.csv`.\n")
    w("| model | sub_target | raw | norm | repeats | excl | ci_low | ci_high |")
    w("|---|---|---|---|---|---|---|---|")
    for _, r in cv.iterrows():
        w(f"| {r['model']} | {r['sub_target']} | {_fmt(r['raw_value'])} | "
          f"{_fmt(r['normalized_score'])} | {int(r['repeats'])} | "
          f"{int(r['excluded_repeats'])} | {_fmt(r['ci_low'])} | {_fmt(r['ci_high'])} |")

    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Layer-2 analysis for the axis-1 campaign.")
    ap.add_argument("--campaign", default=DEFAULT_CAMPAIGN, help="campaign output dir")
    args = ap.parse_args()

    campaign_dir = Path(args.campaign)
    reports = campaign_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    report = build_report(campaign_dir)
    (reports / "layer2_findings.md").write_text(report, encoding="utf-8")

    frames = load_campaign(campaign_dir)
    override = reproducibility_override(frames["tasks"], frames["runs"])
    cv = capability_vectors(frames["tasks"], frames["runs"], reproducibility_override=override)
    cv.to_csv(reports / "capability_vectors.csv", index=False)

    print(f"[layer2] report  -> {reports / 'layer2_findings.md'}")
    print(f"[layer2] vectors -> {reports / 'capability_vectors.csv'} ({len(cv)} rows)")


if __name__ == "__main__":
    main()
