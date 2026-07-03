"""Layer-2 analysis tests: reproducibility, classification, capability vectors."""

from __future__ import annotations

import pandas as pd

from agora.observe.analysis import build_runs_df, build_tasks_df
from agora.observe.jsonl import (
    ArmSpec,
    PostconditionOutcome,
    ProfileSnapshot,
    RunRecord,
    TaskRecord,
)
from agora.observe.layer2 import (
    AXIS_TOOL_CALL_FIDELITY,
    TRAJECTORY_FIELDS,
    capability_vectors,
    classify,
    classify_models,
    model_metrics,
    reproducibility_by_cell,
    trajectory_signatures,
)

# ------------------------------------------------------------------ fixture builders

_MODELS = {
    "qwen-coder-7b": "ollama/qwen2.5-coder:7b",
    "gemma-e4b": "ollama/gemma4:e4b",
    "qwen3-30b": "ollama/qwen3:30b",
    "mistral-nemo-12b": "ollama/mistral-nemo:12b-instruct-2407-q4_K_M",
}
_TASK_IDS = ("small_chain", "loop_depth", "content_robustness")


def _run(cid: str, profile: str, *, scaffolding: str, passed: int, failed: int,
         strategy: str | None = None) -> RunRecord:
    return RunRecord(
        strategy=strategy,
        run_id=f"uuid-{cid}",
        started_at="2026-07-01T08:00:00+00:00",
        ended_at="2026-07-01T08:05:00+00:00",
        duration_s=300.0,
        probe_name="tool-call-fidelity",
        flow_path="flows/tool-call-fidelity.plan.yaml",
        project_name="tool-call-fidelity",
        profile=ProfileSnapshot(
            name=profile, model=_MODELS[profile], num_ctx=8192, max_tokens=2048,
            temperature=0.0, seed=42, keep_alive="30m",
        ),
        arm=ArmSpec(scaffolding=scaffolding, strictness="strict"),
        success=(failed == 0), exit_code=(0 if failed == 0 else 1),
        tasks_total=passed + failed, tasks_passed=passed, tasks_failed=failed,
        tasks_first_pass=passed, async_leak_hits=0, model_offloaded=None,
        tokens_in=0, tokens_out=0, ollama_version="0", git_commit="abc", host="h",
    )


def _task(cid: str, task_id: str, idx: int, *, status, structured, fallback,
          malformed=0, iters=3, first_ftf=None) -> TaskRecord:
    return TaskRecord(
        run_id=f"uuid-{cid}", task_id=task_id, task_index=idx, role="probe_impl",
        task_kind="code_body", status=status, first_pass=(status == "passed"),
        loopback_count=0, iterations=iters,
        postconditions=[PostconditionOutcome(name="ok", passed=(status == "passed"))],
        tool_calls_total=structured + fallback, tool_calls_structured=structured,
        tool_calls_text_fallback=fallback, tool_calls_malformed=malformed,
        tool_call_unknown_name=0, turns_with_text_fallback=(1 if fallback else 0),
        first_text_fallback_iteration=first_ftf, failure_category=(None if status == "passed" else "postcondition"),
        failure_detail=None, duration_s=1.0,
    )


def _frames(runs: list[RunRecord], tasks: list[TaskRecord], repeats: dict[str, int]) -> pd.DataFrame:
    """Build tasks_df through the Layer-1 builders (campaign_run_id via a fake plan)."""

    class _PlanEntry:
        def __init__(self, cid, profile, arm, repeat, run_id):
            self.id, self.profile, self.arm, self.repeat, self.run_id = cid, profile, arm, repeat, run_id

    plan = [
        _PlanEntry(r.run_id.removeprefix("uuid-"), r.profile.name, r.arm, repeats[r.run_id], r.run_id)
        for r in runs
    ]
    runs_df = build_runs_df(runs, plan=plan, campaign_name="test")
    tasks_df = build_tasks_df(tasks, runs_df)
    return runs_df, tasks_df


def _model_runs(profile: str, cids: list[str], task_specs,
                strategy: str | None = None) -> tuple[list, list, dict]:
    """task_specs: list (one per cid) of {task_id: (status, structured, fallback, malformed)}."""
    runs, tasks, reps = [], [], {}
    for rep, (cid, spec) in enumerate(zip(cids, task_specs, strict=True), start=1):
        arm = "lean" if rep <= 3 else "rich"
        passed = sum(1 for v in spec.values() if v[0] == "passed")
        r = _run(cid, profile, scaffolding=arm, passed=passed, failed=len(spec) - passed,
                 strategy=strategy)
        runs.append(r)
        reps[r.run_id] = rep if rep <= 3 else rep - 3
        for idx, tid in enumerate(_TASK_IDS):
            st, s, f, *rest = spec[tid]
            mal = rest[0] if rest else 0
            tasks.append(_task(cid, tid, idx, status=st, structured=s, fallback=f, malformed=mal))
    return runs, tasks, reps


# ------------------------------------------------------------------ trajectory / reproducibility


def test_trajectory_signature_fields_and_na_normalisation() -> None:
    runs, tasks, reps = _model_runs(
        "gemma-e4b", ["r1"],
        [{"small_chain": ("passed", 3, 0), "loop_depth": ("failed", 0, 0),
          "content_robustness": ("passed", 2, 0)}],
    )
    _, tdf = _frames(runs, tasks, reps)
    sigs = trajectory_signatures(tdf)
    assert len(sigs) == 3
    # signature is TRAJECTORY_FIELDS in order; loop_depth row has 0 calls.
    ld = tdf[tdf["task_id"] == "loop_depth"].index[0]
    assert sigs[ld] == ("failed", 3, 0, 0, 0, 0)  # status, iters, total, struct, fallback, malformed
    assert len(sigs[ld]) == len(TRAJECTORY_FIELDS)


def test_reproducibility_identical_repeats_are_reproducible() -> None:
    spec = {"small_chain": ("passed", 3, 0), "loop_depth": ("failed", 0, 0),
            "content_robustness": ("passed", 2, 0)}
    runs, tasks, reps = _model_runs("gemma-e4b", ["a", "b", "c"], [spec, spec, spec])
    _, tdf = _frames(runs, tasks, reps)
    rc = reproducibility_by_cell(tdf)
    assert (rc["reproducible"]).all()
    assert (rc["n_distinct_trajectories"] == 1).all()
    assert (rc["modal_share"] == 1.0).all()


def test_reproducibility_flags_divergent_small_chain() -> None:
    # qwen3-style bistability: small_chain alternates 7-call-pass / 0-call-fail.
    pass_spec = {"small_chain": ("passed", 7, 0), "loop_depth": ("failed", 0, 0),
                 "content_robustness": ("failed", 2, 0)}
    fail_spec = {"small_chain": ("failed", 0, 0), "loop_depth": ("failed", 0, 0),
                 "content_robustness": ("failed", 2, 0)}
    runs, tasks, reps = _model_runs(
        "qwen3-30b", ["a", "b", "c", "d"], [pass_spec, fail_spec, pass_spec, fail_spec]
    )
    _, tdf = _frames(runs, tasks, reps)
    rc = reproducibility_by_cell(tdf).set_index("task_id")
    assert rc.loc["small_chain", "n_distinct_trajectories"] == 2
    assert not rc.loc["small_chain", "reproducible"]
    assert rc.loc["small_chain", "modal_share"] == 0.5
    # the other two tasks are deterministic.
    assert rc.loc["loop_depth", "reproducible"]
    assert rc.loc["content_robustness", "reproducible"]


# ------------------------------------------------------------------ classification


def _metrics_for(profile: str, specs) -> pd.Series:
    cids = [f"{profile}-{i}" for i in range(len(specs))]
    runs, tasks, reps = _model_runs(profile, cids, specs)
    _, tdf = _frames(runs, tasks, reps)
    return model_metrics(tdf).iloc[0]


def test_classify_structured_succeeds() -> None:
    # gemma: all structured, passes 2/3 consistently, reproducible.
    spec = {"small_chain": ("passed", 4, 0), "loop_depth": ("failed", 0, 0),
            "content_robustness": ("passed", 3, 0)}
    assert classify(_metrics_for("gemma-e4b", [spec] * 6)) == "structured-succeeds"


def test_classify_structured_fragile_when_flaky() -> None:
    # structured, but small_chain non-deterministic and low pass rate.
    p = {"small_chain": ("passed", 5, 0), "loop_depth": ("failed", 0, 0),
         "content_robustness": ("failed", 2, 0)}
    f = {"small_chain": ("failed", 0, 0), "loop_depth": ("failed", 0, 0),
         "content_robustness": ("failed", 2, 0)}
    assert classify(_metrics_for("qwen3-30b", [p, f, p, f, p, f])) == "structured-fragile"


def test_classify_narrate_fallback() -> None:
    # coder: emits only text-fallback calls, never structured, passes nothing.
    spec = {"small_chain": ("failed", 0, 1), "loop_depth": ("failed", 0, 0),
            "content_robustness": ("failed", 0, 1)}
    assert classify(_metrics_for("qwen-coder-7b", [spec] * 6)) == "narrate-fallback"


def test_classify_mixed_fails() -> None:
    # mistral: ~50/50 structured vs fallback, zero passes.
    spec = {"small_chain": ("failed", 1, 1), "loop_depth": ("failed", 0, 0),
            "content_robustness": ("failed", 1, 1)}
    assert classify(_metrics_for("mistral-nemo-12b", [spec] * 6)) == "mixed-fails"


def test_classify_inert_when_no_calls() -> None:
    spec = {"small_chain": ("failed", 0, 0), "loop_depth": ("failed", 0, 0),
            "content_robustness": ("failed", 0, 0)}
    assert classify(_metrics_for("qwen3-30b", [spec] * 6)) == "inert"


def test_classify_models_returns_row_per_model() -> None:
    g = {"small_chain": ("passed", 4, 0), "loop_depth": ("failed", 0, 0),
         "content_robustness": ("passed", 3, 0)}
    c = {"small_chain": ("failed", 0, 1), "loop_depth": ("failed", 0, 0),
         "content_robustness": ("failed", 0, 1)}
    gr, gt, grep = _model_runs("gemma-e4b", [f"g{i}" for i in range(6)], [g] * 6)
    cr, ct, crep = _model_runs("qwen-coder-7b", [f"c{i}" for i in range(6)], [c] * 6)
    _, tdf = _frames(gr + cr, gt + ct, {**grep, **crep})
    out = classify_models(tdf).set_index("model")
    assert set(out["behavioral_class"]) == {"structured-succeeds", "narrate-fallback"}


# ------------------------------------------------------------------ capability vectors


def test_capability_vectors_locked_schema_and_rows() -> None:
    spec = {"small_chain": ("passed", 4, 0), "loop_depth": ("failed", 0, 0),
            "content_robustness": ("passed", 3, 0)}
    runs, tasks, reps = _model_runs("gemma-e4b", [f"g{i}" for i in range(6)], [spec] * 6)
    rdf, tdf = _frames(runs, tasks, reps)
    cv = capability_vectors(tdf, rdf, campaign="axis-1")
    assert list(cv.columns) == [
        "campaign", "model", "strategy", "axis", "sub_target", "raw_value",
        "normalized_score", "repeats", "excluded_repeats", "ci_low", "ci_high",
    ]
    # one row per sub_target for the single model.
    assert len(cv) == 7
    assert (cv["axis"] == AXIS_TOOL_CALL_FIDELITY).all()
    assert (cv["campaign"] == "axis-1").all()
    assert cv["strategy"].isna().all()  # no strategy set → null (v1 / control)
    assert (cv["repeats"] == 6).all()  # lean+rich combine → 6, not 3
    assert (cv["excluded_repeats"] == 0).all()  # nothing excluded without an override
    assert (cv["model"] == "gemma-e4b").all()  # profile name, not the ollama tag
    # reproducibility appended as an ordinary sub_target row.
    assert "trajectory_reproducibility_rate" in set(cv["sub_target"])


def test_capability_vectors_keys_cells_by_strategy() -> None:
    """A v2 model with control + treatment runs yields separate cells per strategy
    (control and treatment must not average into one cell)."""
    spec = {"small_chain": ("passed", 4, 0), "loop_depth": ("failed", 0, 0),
            "content_robustness": ("passed", 3, 0)}
    c_runs, c_tasks, c_reps = _model_runs("gemma-e4b", [f"c{i}" for i in range(3)], [spec] * 3)
    t_runs, t_tasks, t_reps = _model_runs(
        "gemma-e4b", [f"t{i}" for i in range(3)], [spec] * 3, strategy="qwen2_5_coder"
    )
    rdf, tdf = _frames(c_runs + t_runs, c_tasks + t_tasks, {**c_reps, **t_reps})
    cv = capability_vectors(tdf, rdf, campaign="axis-1-v2")
    assert (cv["campaign"] == "axis-1-v2").all()
    # 7 sub_targets × 2 strategy cells (control null + treatment).
    assert len(cv) == 14
    assert set(cv["strategy"].dropna()) == {"qwen2_5_coder"}  # treatment cells
    assert cv["strategy"].isna().any()  # control cells present
    # each strategy cell-set is a full 7 sub_targets.
    assert (cv["strategy"] == "qwen2_5_coder").sum() == 7


def test_capability_vectors_structured_rate_and_normalization() -> None:
    spec = {"small_chain": ("passed", 4, 0), "loop_depth": ("failed", 0, 0),
            "content_robustness": ("passed", 3, 0)}
    runs, tasks, reps = _model_runs("gemma-e4b", [f"g{i}" for i in range(6)], [spec] * 6)
    rdf, tdf = _frames(runs, tasks, reps)
    cv = capability_vectors(tdf, rdf).set_index("sub_target")
    # all calls structured ⇒ rate 1.0, identity-normalized.
    assert cv.loc["structured_emission_rate", "raw_value"] == 1.0
    assert cv.loc["structured_emission_rate", "normalized_score"] == 1.0
    # text_fallback_rate: direction not locked ⇒ normalized null, raw present.
    assert cv.loc["text_fallback_rate", "raw_value"] == 0.0
    assert pd.isna(cv.loc["text_fallback_rate", "normalized_score"])


def test_capability_vectors_reproducibility_override() -> None:
    # a divergent model whose steady-state (excl. block-first) reproducibility
    # is reported via the override, with excluded_repeats=1.
    p = {"small_chain": ("passed", 5, 0), "loop_depth": ("failed", 0, 0),
         "content_robustness": ("failed", 2, 0)}
    f = {"small_chain": ("failed", 0, 0), "loop_depth": ("failed", 0, 0),
         "content_robustness": ("failed", 2, 0)}
    runs, tasks, reps = _model_runs(
        "qwen3-30b", [f"q{i}" for i in range(6)], [p, f, p, f, p, f]
    )
    rdf, tdf = _frames(runs, tasks, reps)
    ollama_tag = _MODELS["qwen3-30b"]
    cv = capability_vectors(
        tdf, rdf, reproducibility_override={ollama_tag: (1.0, 1)}
    ).set_index("sub_target")
    repro = cv.loc["trajectory_reproducibility_rate"]
    assert repro["raw_value"] == 1.0
    assert repro["normalized_score"] == 1.0  # higher-is-better ⇒ identity-normalized
    assert repro["excluded_repeats"] == 1
    assert repro["repeats"] == 6  # design count unchanged; excluded tracked separately
    # non-overridden sub_targets keep excluded_repeats=0.
    assert cv.loc["pass_rate", "excluded_repeats"] == 0


def test_capability_vectors_null_first_fallback_when_never_fell_back() -> None:
    spec = {"small_chain": ("passed", 4, 0), "loop_depth": ("failed", 0, 0),
            "content_robustness": ("passed", 3, 0)}
    runs, tasks, reps = _model_runs("gemma-e4b", [f"g{i}" for i in range(6)], [spec] * 6)
    rdf, tdf = _frames(runs, tasks, reps)
    cv = capability_vectors(tdf, rdf).set_index("sub_target")
    row = cv.loc["first_fallback_iteration_p50"]
    assert pd.isna(row["raw_value"]) and pd.isna(row["normalized_score"])
    assert row["repeats"] == 6  # repeats reported even when the value is undefined


def test_empty_frames_yield_typed_empties() -> None:
    empty = pd.DataFrame(
        columns=["model", "task_id", "campaign_run_id", "status",
                 "tool_calls_total", "tool_calls_structured", "tool_calls_text_fallback",
                 "tool_calls_malformed", "iterations", "first_text_fallback_iteration"]
    )
    assert reproducibility_by_cell(empty).empty
    assert model_metrics(empty).empty
    assert capability_vectors(empty).empty
    assert trajectory_signatures(empty).empty


def test_first_fallback_p50_computed_when_present() -> None:
    # two fallback turns at iterations 1 and 3 ⇒ p50 == 2.0.
    spec_a = {"small_chain": ("failed", 0, 1), "loop_depth": ("failed", 0, 0),
              "content_robustness": ("failed", 0, 0)}
    runs, tasks, reps = _model_runs("qwen-coder-7b", ["a", "b"], [spec_a, spec_a])
    # patch first_text_fallback_iteration on the small_chain rows.
    for t in tasks:
        if t.task_id == "small_chain":
            t.first_text_fallback_iteration = 1 if t.run_id.endswith("a") else 3
    _, tdf = _frames(runs, tasks, reps)
    mm = model_metrics(tdf).iloc[0]
    assert mm["first_fallback_p50"] == 2.0
