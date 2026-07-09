"""Capability matrix derivation + CSV store (L1-A).

The load-bearing case is the TWO-ARM battery: one model under a production and a
raw-control harness. They share model/battery/probe/daemon but differ in
harness_hash, so they must land in SEPARATE keyed cells — never pooled.
"""

from __future__ import annotations

import pytest

from agora.bench.keys import KEY_FIELDS, harness_hash
from agora.bench.matrix import (
    MATRIX_COLUMNS,
    append_rows,
    derive_matrix_rows,
    load_matrix,
    query,
    rebuild,
)
from agora.observe.analysis import build_runs_df, build_tasks_df
from agora.observe.jsonl import (
    ArmSpec,
    PostconditionOutcome,
    ProfileSnapshot,
    RunRecord,
    TaskRecord,
)

_DIGEST = "sha256:aaaa1111"
_TASK_IDS = ("small_chain", "loop_depth", "content_robustness")

_PROD = {"tool_errors": "corrective", "nudge_budget": 1, "review_budget": 0}
_RAW = {"tool_errors": "raw", "nudge_budget": 0, "review_budget": 0}


def _run(rid: str, harness: dict, *, digest: str = _DIGEST, battery: str = "standard-v1",
         probe: int | None = 7, daemon: str = "0.1.0") -> RunRecord:
    return RunRecord(
        run_id=rid,
        started_at="2026-07-01T08:00:00+00:00",
        ended_at="2026-07-01T08:05:00+00:00",
        duration_s=300.0,
        probe_name="tool-call-fidelity",
        flow_path="flows/tool-call-fidelity.plan.yaml",
        project_name="tool-call-fidelity",
        profile=ProfileSnapshot(
            name="gemma-e4b", model="ollama/gemma4:e4b", num_ctx=8192, max_tokens=2048,
            temperature=0.0, seed=42, keep_alive="30m",
        ),
        arm=ArmSpec(scaffolding="rich", strictness="strict"),
        success=True, exit_code=0,
        tasks_total=3, tasks_passed=3, tasks_failed=0, tasks_first_pass=3,
        async_leak_hits=0, model_offloaded=None, tokens_in=0, tokens_out=0,
        ollama_version=daemon, git_commit="abc1234", host="p40-box",
        harness=harness, probe_version=probe,
        model_digest=digest, battery_version=battery,
    )


def _task(rid: str, task_id: str, idx: int) -> TaskRecord:
    return TaskRecord(
        run_id=rid, task_id=task_id, task_index=idx, role="probe_impl",
        task_kind="code_body", status="passed", first_pass=True,
        loopback_count=0, iterations=3,
        postconditions=[PostconditionOutcome(name="ok", passed=True)],
        tool_calls_total=3, tool_calls_structured=3, tool_calls_text_fallback=0,
        tool_calls_malformed=0, tool_call_unknown_name=0, turns_with_text_fallback=0,
        first_text_fallback_iteration=None, failure_category=None,
        failure_detail=None, duration_s=1.0,
    )


def _two_arm_fixture():
    """One model, two harness arms (prod + raw), 2 repeats each."""
    runs, tasks = [], []
    for arm_name, harness in (("prod", _PROD), ("raw", _RAW)):
        for rep in (1, 2):
            rid = f"{arm_name}-{rep}"
            runs.append(_run(rid, harness))
            for idx, tid in enumerate(_TASK_IDS):
                tasks.append(_task(rid, tid, idx))
    runs_df = build_runs_df(runs)
    tasks_df = build_tasks_df(tasks, runs_df)
    return runs, runs_df, tasks_df


def test_derive_has_matrix_columns() -> None:
    runs, runs_df, tasks_df = _two_arm_fixture()
    m = derive_matrix_rows(runs, tasks_df, runs_df)
    assert list(m.columns) == list(MATRIX_COLUMNS)
    assert not m.empty


def test_two_arms_land_in_two_distinct_keys() -> None:
    runs, runs_df, tasks_df = _two_arm_fixture()
    m = derive_matrix_rows(runs, tasks_df, runs_df)
    # Same model/battery/probe/daemon, but the harness arms differ ⇒ two keys.
    assert set(m["harness_hash"]) == {harness_hash(_PROD), harness_hash(_RAW)}
    assert m["model_digest"].nunique() == 1 == m["battery_version"].nunique()
    keys = m[list(KEY_FIELDS)].drop_duplicates()
    assert len(keys) == 2  # exactly the two arms, never pooled


def test_key_fields_fully_populated() -> None:
    runs, runs_df, tasks_df = _two_arm_fixture()
    m = derive_matrix_rows(runs, tasks_df, runs_df)
    row = m.iloc[0]
    assert row["model_digest"] == _DIGEST
    assert row["battery_version"] == "standard-v1"
    assert int(row["probe_version"]) == 7
    assert row["daemon_version"] == "0.1.0"
    assert row["date"] == "2026-07-01" and row["hardware"] == "p40-box"
    assert row["git_commit"] == "abc1234" and row["source"] == "local"


def test_derivation_is_deterministic() -> None:
    runs, runs_df, tasks_df = _two_arm_fixture()
    a = derive_matrix_rows(runs, tasks_df, runs_df)
    b = derive_matrix_rows(runs, tasks_df, runs_df)
    from pandas.testing import assert_frame_equal

    assert_frame_equal(rebuild_sorted(a), rebuild_sorted(b))


def rebuild_sorted(df):
    from agora.bench.matrix import ROW_ID_COLUMNS

    return df.sort_values(list(ROW_ID_COLUMNS)).reset_index(drop=True)


def test_append_is_idempotent(tmp_path) -> None:
    runs, runs_df, tasks_df = _two_arm_fixture()
    m = derive_matrix_rows(runs, tasks_df, runs_df)
    csv = str(tmp_path / "matrix.csv")
    first = append_rows(csv, m)
    again = append_rows(csv, m)  # re-deriving the same run must not duplicate
    assert len(first) == len(again)
    # Reloading from disk round-trips the schema.
    assert list(load_matrix(csv).columns) == list(MATRIX_COLUMNS)


def test_rebuild_is_byte_identical(tmp_path) -> None:
    runs, runs_df, tasks_df = _two_arm_fixture()
    m = derive_matrix_rows(runs, tasks_df, runs_df)
    p1, p2 = str(tmp_path / "a.csv"), str(tmp_path / "b.csv")
    rebuild(p1, m)
    rebuild(p2, m)
    assert open(p1, encoding="utf-8").read() == open(p2, encoding="utf-8").read()


def test_incomplete_key_is_rejected() -> None:
    runs, runs_df, tasks_df = _two_arm_fixture()
    runs[0] = _run("prod-1", _PROD, digest="")  # missing digest
    with pytest.raises(ValueError, match="incomplete key"):
        derive_matrix_rows(runs, tasks_df, runs_df)


def test_query_refuses_cross_key_without_optin() -> None:
    runs, runs_df, tasks_df = _two_arm_fixture()
    m = derive_matrix_rows(runs, tasks_df, runs_df)
    # Filtering only by sub_target spans both harness keys ⇒ must raise.
    with pytest.raises(ValueError, match="distinct keys"):
        query(m, sub_target="pass_rate")
    # Opt-in returns both arms' rows.
    both = query(m, sub_target="pass_rate", allow_cross_key=True)
    assert set(both["harness_hash"]) == {harness_hash(_PROD), harness_hash(_RAW)}
    # Narrowing to one harness key is always allowed.
    one = query(m, sub_target="pass_rate", harness_hash=harness_hash(_RAW))
    assert len(one) == 1
