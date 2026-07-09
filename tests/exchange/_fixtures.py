"""Shared submission builders for exchange tests."""

from __future__ import annotations

from agora.bench.matrix import derive_matrix_rows
from agora.exchange.schema import Attestation, build_manifest
from agora.observe.analysis import build_runs_df, build_tasks_df
from agora.observe.jsonl import (
    ArmSpec,
    PostconditionOutcome,
    ProfileSnapshot,
    RunRecord,
    TaskRecord,
)

DIGEST = "sha256:aaaa1111bbbb"
BATTERY = "standard-v1"
_PROD = {"tool_errors": "corrective", "nudge_budget": 1, "review_budget": 0}
_RAW = {"tool_errors": "raw", "nudge_budget": 0, "review_budget": 0}
_TASK_IDS = ("small_chain", "loop_depth", "content_robustness")


def _run(rid: str, harness: dict, *, digest: str = "", battery: str = "", duration: float = 300.0) -> RunRecord:
    return RunRecord(
        run_id=rid, started_at="2026-07-01T08:00:00+00:00",
        ended_at="2026-07-01T08:05:00+00:00", duration_s=duration,
        probe_name="tool-call-fidelity", flow_path="flows/tool-call-fidelity.plan.yaml",
        project_name="tool-call-fidelity",
        profile=ProfileSnapshot(name="gemma-e4b", model="ollama/gemma4:e4b", num_ctx=8192,
                                max_tokens=2048, temperature=0.0, seed=42, keep_alive="30m"),
        arm=ArmSpec(scaffolding="rich", strictness="strict"),
        success=True, exit_code=0, tasks_total=3, tasks_passed=3, tasks_failed=0,
        tasks_first_pass=3, async_leak_hits=0, tokens_in=10, tokens_out=20,
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


def make_records(*, digest: str = "", battery: str = "", duration: float = 300.0):
    runs, tasks = [], []
    for arm_name, harness in (("prod", _PROD), ("raw", _RAW)):
        for rep in (1, 2):
            rid = f"{arm_name}-{rep}"
            runs.append(_run(rid, harness, digest=digest, battery=battery, duration=duration))
            tasks.extend(_task(rid, tid, i) for i, tid in enumerate(_TASK_IDS))
    return runs, tasks


def make_submission(*, duration: float = 300.0):
    """Return (manifest, attestation, run_records, task_records). Records carry no
    digest/battery (the runner records neither) — the attestation supplies them."""
    runs, tasks = make_records(duration=duration)
    stamped = [r.model_copy(update={"model_digest": DIGEST, "battery_version": BATTERY}) for r in runs]
    runs_df = build_runs_df(stamped, campaign_name="sub")
    tasks_df = build_tasks_df(tasks, runs_df)
    manifest = build_manifest(derive_matrix_rows(stamped, tasks_df, runs_df))
    attestation = Attestation(model_digest=DIGEST, battery_version=BATTERY, daemon_version="0.1.0",
                              gpu="Tesla P40", os="Windows", contributor="octocat")
    return manifest, attestation, runs, tasks
