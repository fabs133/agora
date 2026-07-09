"""ingest_run_dir — JSONL run dir -> keyed matrix rows, stamping digest/battery (L1-B)."""

from __future__ import annotations

from agora.bench.ingest import ingest_run_dir
from agora.bench.keys import harness_hash
from agora.observe.jsonl import (
    ArmSpec,
    PostconditionOutcome,
    ProfileSnapshot,
    RunRecord,
    TaskRecord,
)

_PROD = {"tool_errors": "corrective", "nudge_budget": 1, "review_budget": 0}
_RAW = {"tool_errors": "raw", "nudge_budget": 0, "review_budget": 0}
_TASK_IDS = ("small_chain", "loop_depth", "content_robustness")


def _run(rid: str, harness: dict, *, digest: str = "", battery: str = "") -> RunRecord:
    return RunRecord(
        run_id=rid, started_at="2026-07-01T08:00:00+00:00",
        ended_at="2026-07-01T08:05:00+00:00", duration_s=300.0,
        probe_name="tool-call-fidelity", flow_path="flows/tool-call-fidelity.plan.yaml",
        project_name="tool-call-fidelity",
        profile=ProfileSnapshot(name="gemma-e4b", model="ollama/gemma4:e4b", num_ctx=8192,
                                max_tokens=2048, temperature=0.0, seed=42, keep_alive="30m"),
        arm=ArmSpec(scaffolding="rich", strictness="strict"),
        success=True, exit_code=0, tasks_total=3, tasks_passed=3, tasks_failed=0,
        tasks_first_pass=3, async_leak_hits=0, tokens_in=0, tokens_out=0,
        ollama_version="0.1.0", git_commit="abc1234", host="p40-box",
        harness=harness, probe_version=7, model_digest=digest, battery_version=battery,
    )


def _task(rid: str, task_id: str, idx: int) -> TaskRecord:
    return TaskRecord(
        run_id=rid, task_id=task_id, task_index=idx, role="probe_impl", task_kind="code_body",
        status="passed", first_pass=True, loopback_count=0, iterations=3,
        postconditions=[PostconditionOutcome(name="ok", passed=True)],
        tool_calls_total=3, tool_calls_structured=3, tool_calls_text_fallback=0,
        tool_calls_malformed=0, tool_call_unknown_name=0, turns_with_text_fallback=0,
        first_text_fallback_iteration=None, failure_category=None, failure_detail=None,
        duration_s=1.0,
    )


def _write_run_dir(tmp_path, *, digest="", battery=""):
    """Flat single-dir layout: run.jsonl + tasks.jsonl, 2 arms x 2 repeats."""
    runs, tasks = [], []
    for arm_name, harness in (("prod", _PROD), ("raw", _RAW)):
        for rep in (1, 2):
            rid = f"{arm_name}-{rep}"
            runs.append(_run(rid, harness, digest=digest, battery=battery))
            tasks.extend(_task(rid, tid, i) for i, tid in enumerate(_TASK_IDS))
    (tmp_path / "run.jsonl").write_text(
        "\n".join(r.model_dump_json() for r in runs), encoding="utf-8"
    )
    (tmp_path / "tasks.jsonl").write_text(
        "\n".join(t.model_dump_json() for t in tasks), encoding="utf-8"
    )
    return tmp_path


def test_ingest_stamps_digest_and_battery_when_records_lack_them(tmp_path) -> None:
    _write_run_dir(tmp_path)  # records recorded no digest/battery
    m = ingest_run_dir(tmp_path, model_digest="sha256:stamped", battery_version="standard-v1")
    assert set(m["model_digest"]) == {"sha256:stamped"}
    assert set(m["battery_version"]) == {"standard-v1"}
    # Two harness arms -> two distinct keys, never pooled.
    assert set(m["harness_hash"]) == {harness_hash(_PROD), harness_hash(_RAW)}


def test_ingest_prefers_the_records_own_provenance(tmp_path) -> None:
    # A run that recorded its OWN digest keeps it — the stamp only fills blanks.
    _write_run_dir(tmp_path, digest="sha256:recorded", battery="standard-v1")
    m = ingest_run_dir(tmp_path, model_digest="sha256:stamped", battery_version="other-v9")
    assert set(m["model_digest"]) == {"sha256:recorded"}
    assert set(m["battery_version"]) == {"standard-v1"}
