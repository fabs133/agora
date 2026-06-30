"""Layer-1 analysis pipeline tests: load JSONL → tidy DataFrames."""

from __future__ import annotations

import json
import math
import warnings
from pathlib import Path

import pandas as pd
import pytest

from agora.core.errors import AgoraError
from agora.observe.analysis import (
    _RUNS_SCHEMA,
    _TASKS_SCHEMA,
    build_postconditions_df,
    build_runs_df,
    build_tasks_df,
    load_campaign,
    load_run_records,
    model_family,
    model_size_b,
)
from agora.observe.jsonl import (
    ArmSpec,
    PostconditionOutcome,
    ProfileSnapshot,
    RunRecord,
    TaskRecord,
)

FIXTURE = Path(__file__).parent / "fixtures" / "checkpoint1"
CHECKPOINT1_RUN_ID = "eccb2f52b239488a9cc2c72ec279a461"


# ------------------------------------------------------------------ builders


def _run(run_id: str, *, model="ollama/qwen2.5-coder:7b", name="qwen-coder-7b",
         passed=2, failed=2, total=12, scaffolding="rich", **over) -> RunRecord:
    base = dict(
        run_id=run_id,
        started_at="2026-06-30T08:00:00+00:00",
        ended_at="2026-06-30T08:10:00+00:00",
        duration_s=600.0,
        probe_name="probe",
        flow_path="flows/x.yaml",
        project_name="proj",
        profile=ProfileSnapshot(
            name=name, model=model, num_ctx=8192, max_tokens=2048,
            temperature=0.0, seed=42, keep_alive="30m",
        ),
        arm=ArmSpec(scaffolding=scaffolding, strictness="strict"),
        success=True,
        exit_code=0,
        tasks_total=total,
        tasks_passed=passed,
        tasks_failed=failed,
        tasks_first_pass=passed,
        async_leak_hits=0,
        model_offloaded=None,
        tokens_in=0,
        tokens_out=0,
        ollama_version="0",
        git_commit="abc1234",
        host="h",
    )
    base.update(over)
    return RunRecord(**base)


def _task(run_id: str, task_id: str, idx: int, *, status="passed", first_pass=True,
          loopback=0, iters=3, pcs=(("ok", True),), structured=3, fallback=0,
          total=3, **over) -> TaskRecord:
    base = dict(
        run_id=run_id, task_id=task_id, task_index=idx, role="implementer",
        task_kind="code_body", status=status, first_pass=first_pass,
        loopback_count=loopback, iterations=iters,
        postconditions=[PostconditionOutcome(name=n, passed=p) for n, p in pcs],
        tool_calls_total=total, tool_calls_structured=structured,
        tool_calls_text_fallback=fallback, tool_calls_malformed=0,
        tool_call_unknown_name=0, turns_with_text_fallback=0,
        first_text_fallback_iteration=None, failure_category=None,
        failure_detail=None, duration_s=1.0,
    )
    base.update(over)
    return TaskRecord(**base)


def _write_run_dir(d: Path, run: RunRecord, tasks: list[TaskRecord]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.jsonl").write_text(run.model_dump_json() + "\n", encoding="utf-8")
    (d / "tasks.jsonl").write_text(
        "".join(t.model_dump_json() + "\n" for t in tasks), encoding="utf-8"
    )


def _write_campaign(root: Path, entries: list[tuple[str, RunRecord, list[TaskRecord], int]]) -> None:
    """entries: (campaign_id, run, tasks, repeat). Writes per-run dirs + plan.jsonl."""
    plan_lines = []
    for cid, run, tasks, repeat in entries:
        _write_run_dir(root / cid, run, tasks)
        plan_lines.append(json.dumps({
            "id": cid, "probe": "flows/tool-call-fidelity.plan.yaml",
            "profile": run.profile.name, "arm": run.arm.model_dump(),
            "repeat": repeat, "params": {"seed": 42},
        }))
    (root / "plan.jsonl").write_text("\n".join(plan_lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ 1-3: loading


def test_load_single_run_checkpoint1_fixture() -> None:
    runs, tasks, plan = load_run_records(FIXTURE)
    assert len(runs) == 1
    assert len(tasks) == 12
    assert plan is None
    assert runs[0].run_id == CHECKPOINT1_RUN_ID


def test_load_malformed_jsonl_raises_with_path_and_line(tmp_path) -> None:
    _write_run_dir(tmp_path, _run("r"), [_task("r", "t0", 0)])
    # Corrupt line 2 of tasks.jsonl.
    (tmp_path / "tasks.jsonl").write_text(
        _task("r", "t0", 0).model_dump_json() + "\n{ this is not json\n",
        encoding="utf-8",
    )
    with pytest.raises(AgoraError, match=r"tasks\.jsonl:2"):
        load_run_records(tmp_path)


def test_load_campaign_layout(tmp_path) -> None:
    _write_campaign(tmp_path, [
        ("r001", _run("uuid-a", scaffolding="lean"), [_task("uuid-a", "t0", 0)], 1),
        ("r002", _run("uuid-b", scaffolding="rich"), [_task("uuid-b", "t0", 0), _task("uuid-b", "t1", 1)], 1),
    ])
    runs, tasks, plan = load_run_records(tmp_path)
    assert len(runs) == 2
    assert len(tasks) == 3
    assert plan is not None and len(plan) == 2
    # Plan entries enriched with the run uuid discovered in each subdir.
    by_id = {p.id: p for p in plan}
    assert by_id["r001"].run_id == "uuid-a"
    assert by_id["r002"].run_id == "uuid-b"


# ------------------------------------------------------------------ 4-6: runs_df


def test_build_runs_df_columns_and_types() -> None:
    runs, _tasks, plan = load_run_records(FIXTURE)
    df = build_runs_df(runs, plan=plan)
    expected = {
        c: ("datetime64[ns, UTC]" if dt == "datetime" else dt)
        for c, dt in _RUNS_SCHEMA.items()
    }
    assert list(df.columns) == list(_RUNS_SCHEMA)
    for col, dt in expected.items():
        assert str(df[col].dtype) == dt, f"{col}: {df[col].dtype} != {dt}"


def test_build_runs_df_empty_input_returns_typed_empty_frame() -> None:
    df = build_runs_df([])
    assert len(df) == 0
    assert list(df.columns) == list(_RUNS_SCHEMA)
    assert str(df["started_at"].dtype) == "datetime64[ns, UTC]"
    assert str(df["num_ctx"].dtype) == "Int64"
    assert str(df["success"].dtype) == "boolean"


def test_build_runs_df_ratios() -> None:
    runs = [
        _run("ok", passed=3, failed=1, total=12),     # completed 4/12, pass 3/4
        _run("none", passed=0, failed=0, total=0),    # total 0 → both None
        _run("allskip", passed=0, failed=0, total=5),  # completed 0/5, pass 0/0 → None
    ]
    df = build_runs_df(runs).set_index("run_id")
    assert df.loc["ok", "tasks_completed_ratio"] == pytest.approx(4 / 12)
    assert df.loc["ok", "tasks_pass_ratio"] == pytest.approx(3 / 4)
    assert math.isnan(df.loc["none", "tasks_completed_ratio"])
    assert math.isnan(df.loc["none", "tasks_pass_ratio"])
    assert df.loc["allskip", "tasks_completed_ratio"] == pytest.approx(0.0)
    assert math.isnan(df.loc["allskip", "tasks_pass_ratio"])  # divide-by-zero → None


# ------------------------------------------------------------------ 7-8: tasks_df


def test_build_tasks_df_skipped_rows_have_null_first_pass() -> None:
    """The commit-0 nullability must survive the DataFrame round-trip.

    Synthesized post-commit-0 skip record (first_pass/loopback/iterations =
    null). The committed Checkpoint-1 fixture predates that fix (git_commit
    2176a25) and carries false/0, so it can't exercise this path.
    """
    runs_df = build_runs_df([_run("r")])
    skipped = _task(
        "r", "skipped_task", 0, status="skipped",
        first_pass=None, loopback=None, iters=None, pcs=(),
    )
    passed = _task("r", "ran_task", 1)
    tasks_df = build_tasks_df([skipped, passed], runs_df).set_index("task_id")
    for col in ("first_pass", "loopback_count", "iterations"):
        assert pd.isna(tasks_df.loc["skipped_task", col])
        # The non-skipped row keeps its concrete value.
        assert not pd.isna(tasks_df.loc["ran_task", col])
    assert str(tasks_df["first_pass"].dtype) == "boolean"
    assert str(tasks_df["iterations"].dtype) == "Int64"


def test_build_tasks_df_drops_orphan_task_run_id() -> None:
    runs_df = build_runs_df([_run("real")])
    tasks = [_task("real", "t0", 0), _task("ghost", "t1", 0)]
    with pytest.warns(UserWarning, match="ghost"):
        tasks_df = build_tasks_df(tasks, runs_df)
    assert len(tasks_df) == 1
    assert tasks_df["run_id"].tolist() == ["real"]


def test_build_tasks_df_columns_and_types() -> None:
    runs_df = build_runs_df([_run("r")])
    tasks_df = build_tasks_df([_task("r", "t0", 0)], runs_df)
    expected = {
        c: ("datetime64[ns, UTC]" if dt == "datetime" else dt)
        for c, dt in _TASKS_SCHEMA.items()
    }
    assert list(tasks_df.columns) == list(_TASKS_SCHEMA)
    for col, dt in expected.items():
        assert str(tasks_df[col].dtype) == dt, f"{col}: {tasks_df[col].dtype} != {dt}"


# ------------------------------------------------------------------ 9: postconditions_df


def test_build_postconditions_df_empty_array_no_rows() -> None:
    tasks = [
        _task("r", "has_pcs", 0, pcs=(("a", True), ("b", False))),
        _task("r", "no_pcs", 1, pcs=()),
    ]
    df = build_postconditions_df(tasks)
    assert len(df) == 2  # only the two from has_pcs; none from no_pcs
    assert df["task_id"].tolist() == ["has_pcs", "has_pcs"]
    assert df["postcondition_name"].tolist() == ["a", "b"]
    assert df["passed"].tolist() == [True, False]
    assert str(df["passed"].dtype) == "boolean"


# ------------------------------------------------------------------ 10-11: derived


@pytest.mark.parametrize("model,expected", [
    ("ollama/qwen2.5-coder:7b", "qwen-coder"),
    ("ollama/qwen2.5-coder:14b", "qwen-coder"),
    ("ollama/qwen2.5:7b-instruct", "qwen-instruct"),
    ("ollama/gemma4:e4b", "gemma"),
    ("ollama/mistral-nemo:12b", "mistral"),
    ("ollama/qwen3:30b", "qwen-thinking"),
    ("ollama/llama3.1:8b", "unknown"),
])
def test_model_family_classification(model, expected) -> None:
    assert model_family(model) == expected


@pytest.mark.parametrize("model,expected", [
    ("ollama/qwen2.5-coder:7b", 7.0),
    ("ollama/qwen2.5-coder:14b", 14.0),
    ("ollama/qwen3:30b", 30.0),
    ("ollama/mistral-nemo:12b", 12.0),
    ("ollama/gemma4:e4b", 4.0),
    ("ollama/qwen2.5:7b-instruct", 7.0),
])
def test_model_size_b_parsing(model, expected) -> None:
    assert model_size_b(model) == expected


def test_model_size_b_unparseable_is_nan() -> None:
    assert math.isnan(model_size_b("ollama/something:latest"))


def test_unknown_model_warns_once_per_string() -> None:
    runs = [_run("a", model="ollama/mystery:latest", name="m"),
            _run("b", model="ollama/mystery:latest", name="m")]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_runs_df(runs)
    msgs = [str(w.message) for w in caught]
    # One family warning + one size warning for the single distinct string.
    assert sum("unrecognised model family" in m for m in msgs) == 1
    assert sum("could not parse model size" in m for m in msgs) == 1


# ------------------------------------------------------------------ 12-13: invariants


def test_invariant_tool_calls_structured_plus_fallback_equals_total() -> None:
    d = load_campaign(FIXTURE)
    t = d["tasks"]
    assert (
        t["tool_calls_structured"] + t["tool_calls_text_fallback"]
        == t["tool_calls_total"]
    ).all()


def test_checkpoint1_roundtrip_no_warnings() -> None:
    """Acceptance #5: load → build all three → no warnings, twelve task rows."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        d = load_campaign(FIXTURE)
    assert len(d["runs"]) == 1
    assert len(d["tasks"]) == 12
    # postconditions: one row per (task, postcondition) on non-skipped tasks.
    runs, tasks, _ = load_run_records(FIXTURE)
    expected_pc = sum(len(t.postconditions) for t in tasks)
    assert len(d["postconditions"]) == expected_pc


def test_load_campaign_full_roundtrip(tmp_path) -> None:
    entries = [
        (f"r00{i}", _run(f"uuid-{i}", scaffolding="lean" if i < 3 else "rich"),
         [_task(f"uuid-{i}", "t0", 0), _task(f"uuid-{i}", "t1", 1, pcs=())], (i % 3) + 1)
        for i in range(1, 5)
    ]
    _write_campaign(tmp_path, entries)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        d = load_campaign(tmp_path)
    runs_df, tasks_df, pc_df = d["runs"], d["tasks"], d["postconditions"]
    assert len(runs_df) == 4
    assert len(tasks_df) == 8  # 4 runs × 2 tasks
    # campaign_name = output dir basename; campaign_run_id + repeat recovered.
    assert (runs_df["campaign_name"] == tmp_path.name).all()
    assert set(runs_df["campaign_run_id"]) == {"r001", "r002", "r003", "r004"}
    # Clean join: every task run_id is in runs_df, no orphans, no dupes.
    run_ids = set(runs_df["run_id"])
    assert set(tasks_df["run_id"]).issubset(run_ids)
    assert not tasks_df["run_id"].isna().any()
    # postconditions: only t0 carries pcs (t1 has none) → 4 rows.
    assert len(pc_df) == 4
    assert set(pc_df["run_id"]).issubset(run_ids)
