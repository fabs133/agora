"""Tests for the staged sweep wrapper: stage→runs mapping, report generation,
--report-only side-effect freedom, and partial-output headers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agora.observe.jsonl import (
    ArmSpec,
    PostconditionOutcome,
    ProfileSnapshot,
    RunRecord,
    TaskRecord,
)
from scripts import run_sweep_staged as sweep
from scripts.run_campaign import load_campaign

COMMITTED = "campaigns/axis-1-tool-call-fidelity.yaml"


# ------------------------------------------------------------- stage → runs mapping


@pytest.mark.parametrize("stage,count,first,last", [
    ("1", 1, "r001", "r001"),
    ("2", 6, "r001", "r006"),
    ("3", 12, "r001", "r012"),
    ("4a", 30, "r001", "r030"),
    ("4b", 36, "r001", "r036"),
])
def test_stage_target_ids(stage, count, first, last) -> None:
    campaign = load_campaign(COMMITTED)
    ids = sweep.stage_target_ids(campaign, stage)
    assert len(ids) == count
    assert ids[0] == first
    assert ids[-1] == last
    # Contiguous prefix of the declared order.
    assert ids == [f"r{i:03d}" for i in range(1, count + 1)]


def test_stage_aliases() -> None:
    assert sweep._STAGE_ALIASES["4"] == ["4a", "4b"]
    assert sweep._STAGE_ALIASES["all"] == ["1", "2", "3", "4a", "4b"]


# ------------------------------------------------------------- synthetic campaign dir


def _run(run_id, model, name, scaffolding, *, passed=3, failed=0, total=3):
    return RunRecord(
        run_id=run_id, started_at="2026-07-01T00:00:00+00:00",
        ended_at="2026-07-01T00:01:00+00:00", duration_s=60.0,
        probe_name="tool-call-fidelity", flow_path="flows/tool-call-fidelity.plan.yaml",
        project_name="tool-call-fidelity",
        profile=ProfileSnapshot(name=name, model=model, num_ctx=8192, max_tokens=2048,
                                temperature=0.0, seed=42, keep_alive="30m"),
        arm=ArmSpec(scaffolding=scaffolding, strictness="strict"),
        success=(failed == 0), exit_code=0, tasks_total=total, tasks_passed=passed,
        tasks_failed=failed, tasks_first_pass=passed, async_leak_hits=0,
        model_offloaded=None, tokens_in=100, tokens_out=50, ollama_version="0.24.0",
        git_commit="abc1234", host="h",
    )


def _task(run_id, task_id, idx, *, status="passed", structured=3, fallback=0):
    return TaskRecord(
        run_id=run_id, task_id=task_id, task_index=idx, role="implementer",
        task_kind="code_body", status=status,
        first_pass=(status == "passed"), loopback_count=0, iterations=3,
        postconditions=[PostconditionOutcome(name="mark_complete_called", passed=(status == "passed"))],
        tool_calls_total=structured + fallback, tool_calls_structured=structured,
        tool_calls_text_fallback=fallback, tool_calls_malformed=0,
        tool_call_unknown_name=0, turns_with_text_fallback=(1 if fallback else 0),
        first_text_fallback_iteration=(0 if fallback else None),
        failure_category=(None if status == "passed" else "postcondition"),
        failure_detail=(None if status == "passed" else "x"), duration_s=20.0,
    )


def _write_campaign_dir(root: Path, entries):
    """entries: (campaign_id, model, name, scaffolding, repeat). Writes plan.jsonl
    + per-run subdirs (run.jsonl + tasks.jsonl, 3 probe tasks each)."""
    plan_lines = []
    for cid, model, name, scaffolding, repeat in entries:
        d = root / cid
        d.mkdir(parents=True, exist_ok=True)
        run = _run(f"uuid-{cid}", model, name, scaffolding)
        (d / "run.jsonl").write_text(run.model_dump_json() + "\n", encoding="utf-8")
        tasks = [
            _task(f"uuid-{cid}", "small_chain", 0),
            _task(f"uuid-{cid}", "loop_depth", 1),
            _task(f"uuid-{cid}", "content_robustness", 2),
        ]
        (d / "tasks.jsonl").write_text(
            "".join(t.model_dump_json() + "\n" for t in tasks), encoding="utf-8"
        )
        plan_lines.append(json.dumps({
            "id": cid, "probe": "flows/tool-call-fidelity.plan.yaml", "profile": name,
            "arm": {"scaffolding": scaffolding, "strictness": "strict"},
            "repeat": repeat, "params": {"seed": 42},
        }))
    (root / "plan.jsonl").write_text("\n".join(plan_lines) + "\n", encoding="utf-8")


def _tiny_campaign_yaml(tmp_path: Path, output_dir: Path) -> Path:
    text = f"""
schema_version: 1
name: axis-1-tool-call-fidelity
defaults:
  params: {{temperature: 0.0, seed: 42, num_ctx: 8192, max_tokens: 2048}}
  output_dir: {output_dir.as_posix()}
  resume: true
runs:
"""
    for i in range(1, 7):
        scaffolding = "lean" if i <= 3 else "rich"
        text += (
            f"  - {{id: r{i:03d}, probe: flows/tool-call-fidelity.plan.yaml, "
            f"profile: qwen-coder-7b, arm: {{scaffolding: {scaffolding}, strictness: strict}}, "
            f"repeat: {((i - 1) % 3) + 1}}}\n"
        )
    p = tmp_path / "camp.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------- report generation


def test_report_generation_on_synthetic_dir(tmp_path) -> None:
    out = tmp_path / "out"
    _write_campaign_dir(out, [
        ("r001", "ollama/qwen2.5-coder:7b", "qwen-coder-7b", "lean", 1),
        ("r002", "ollama/qwen2.5-coder:7b", "qwen-coder-7b", "lean", 2),
        ("r003", "ollama/qwen2.5-coder:7b", "qwen-coder-7b", "lean", 3),
    ])
    camp = _tiny_campaign_yaml(tmp_path, out)
    campaign = load_campaign(camp)
    md = sweep.generate_report("2", out, campaign, str(camp))
    # Structure + traceability markers.
    assert "Stage 2: Repeat agreement" in md
    assert "## Schema validation" in md
    assert "## Tool-call invariant" in md
    assert "Repeat agreement" in md
    assert "Lean vs rich" in md
    # Traceable: every metric shows the pandas expression in backticks.
    assert "`(t['tool_calls_structured'] + t['tool_calls_text_fallback'] == t['tool_calls_total']).all()` → **True**" in md
    # 3 of 6 targets present (only r001-r003 written).
    assert "**3 of 6**" in md


def test_report_partial_output_header(tmp_path) -> None:
    """A partial dir (some target runs missing) shows N of M in the header."""
    out = tmp_path / "out"
    _write_campaign_dir(out, [
        ("r001", "ollama/qwen2.5-coder:7b", "qwen-coder-7b", "lean", 1),
        ("r002", "ollama/qwen2.5-coder:7b", "qwen-coder-7b", "lean", 2),
    ])
    camp = _tiny_campaign_yaml(tmp_path, out)
    campaign = load_campaign(camp)
    md = sweep.generate_report("2", out, campaign, str(camp))
    assert "**2 of 6**" in md


def test_report_layer1_error_is_surfaced(tmp_path, monkeypatch) -> None:
    """A Layer-1 failure is shown verbatim, never silently swallowed."""
    out = tmp_path / "out"
    out.mkdir()

    def _boom(_dir):
        raise ValueError("synthetic layer-1 explosion")

    monkeypatch.setattr(sweep.analysis, "load_campaign", _boom)
    campaign = load_campaign(_tiny_campaign_yaml(tmp_path, out))
    md = sweep.generate_report("1", out, campaign, "camp.yaml")
    assert "Layer 1 raised" in md
    assert "synthetic layer-1 explosion" in md


def test_stage_4b_report_notes_missing_qwen3(tmp_path) -> None:
    out = tmp_path / "out"
    _write_campaign_dir(out, [
        ("r001", "ollama/qwen2.5-coder:7b", "qwen-coder-7b", "lean", 1),
    ])
    camp = _tiny_campaign_yaml(tmp_path, out)
    campaign = load_campaign(camp)
    md = sweep.generate_report("4b", out, campaign, str(camp))
    assert "qwen3-30b <think>-block handling" in md
    # No qwen-thinking rows → degrades cleanly rather than fabricating.
    assert "no qwen-thinking rows present" in md


def test_stage_4b_qwen3_present_states_content_len_limitation(tmp_path) -> None:
    out = tmp_path / "out"
    _write_campaign_dir(out, [
        ("r001", "ollama/qwen3:30b", "qwen3-30b", "lean", 1),
    ])
    camp = _tiny_campaign_yaml(tmp_path, out)
    campaign = load_campaign(camp)
    md = sweep.generate_report("4b", out, campaign, str(camp))
    # content_len limitation stated, not fabricated (it's not a schema-v1 field).
    assert "content_len` is not a schema-v1 field" in md
    assert "tool_calls_text_fallback" in md


# ------------------------------------------------------------- --report-only


def test_report_only_no_execution(tmp_path, monkeypatch) -> None:
    """--report-only must not launch run_campaign (no subprocess)."""
    out = tmp_path / "out"
    _write_campaign_dir(out, [
        ("r001", "ollama/qwen2.5-coder:7b", "qwen-coder-7b", "lean", 1),
    ])
    camp = _tiny_campaign_yaml(tmp_path, out)

    called = {"n": 0}

    def _boom_run(*_a, **_k):
        called["n"] += 1
        raise AssertionError("subprocess.run must not be called under --report-only")

    monkeypatch.setattr(sweep.subprocess, "run", _boom_run)
    rc = sweep.main([
        "--campaign", str(camp), "--stage", "1", "--report-only",
        "--output-dir", str(out),
    ])
    assert called["n"] == 0
    assert rc == 0
    # Report was written to the reports/ subdir.
    assert (out / "reports" / "stage_1.md").is_file()


def test_report_only_exit_1_on_recorded_failure(tmp_path, monkeypatch) -> None:
    out = tmp_path / "out"
    _write_campaign_dir(out, [
        ("r001", "ollama/qwen2.5-coder:7b", "qwen-coder-7b", "lean", 1),
    ])
    # Overwrite r001 with a success:false record.
    failed = _run("uuid-r001", "ollama/qwen2.5-coder:7b", "qwen-coder-7b", "lean",
                  passed=1, failed=2)
    (out / "r001" / "run.jsonl").write_text(failed.model_dump_json() + "\n", encoding="utf-8")
    camp = _tiny_campaign_yaml(tmp_path, out)
    monkeypatch.setattr(sweep.subprocess, "run", lambda *_a, **_k: pytest.fail("no exec"))
    rc = sweep.main([
        "--campaign", str(camp), "--stage", "1", "--report-only", "--output-dir", str(out),
    ])
    assert rc == 1  # a recorded success:false in the target set → exit 1
