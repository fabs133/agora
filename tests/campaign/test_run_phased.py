"""Phase-staged runner (scripts/run_phased.py) — pure state/gate/waiver logic.

The live orchestration path (run_phase) is NOT exercised here: the real flow is
executed only in the paired session. These tests pin the resumable state machine,
the refuse-on-red discipline, waiver round-trip, rerun re-evaluation, and the
never-reorder invariant.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "run_phased", Path(__file__).resolve().parents[2] / "scripts" / "run_phased.py"
)
rp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rp)

PHASES = ["P3", "P4", "P5", "P6", "P7", "P9"]


def _rec(phase, passed, blockers=(), tasks=()):
    return {"phase": phase, "passed": passed, "blockers": list(blockers), "tasks": list(tasks)}


# --------------------------------------------------------------- resume / next

def test_next_runs_first_pending() -> None:
    states = rp.phase_states(PHASES, [], [])
    assert rp.next_action(states) == ("run", "P3", None)


def test_resume_skips_green_phases() -> None:
    records = [_rec("P3", True), _rec("P4", True)]
    states = rp.phase_states(PHASES, records, [])
    assert states[0] == ("P3", "green")
    assert states[1] == ("P4", "green")
    assert rp.next_action(states)[:2] == ("run", "P5")


def test_next_refuses_on_unwaived_red() -> None:
    records = [_rec("P3", True), _rec("P4", False, blockers=["T4.1"])]
    kind, phase, msg = rp.next_action(rp.phase_states(PHASES, records, []))
    assert kind == "refuse"
    assert phase == "P4"
    assert "red and unwaived" in msg


def test_all_green_is_done() -> None:
    records = [_rec(p, True) for p in PHASES]
    assert rp.next_action(rp.phase_states(PHASES, records, []))[0] == "done"


# --------------------------------------------------------------- waiver round-trip

def test_waiver_round_trip_enables_next(tmp_path) -> None:
    out = tmp_path
    phases_path = out / "phases.jsonl"
    waivers_path = out / "waivers.jsonl"
    phases_path.write_text(
        "\n".join(
            __import__("json").dumps(r)
            for r in [_rec("P3", True), _rec("P4", False, blockers=["T4.1"])]
        )
        + "\n",
        encoding="utf-8",
    )
    records = rp.load_jsonl(phases_path)
    # Before waiver: refuse at P4.
    assert rp.next_action(rp.phase_states(PHASES, records, []))[0] == "refuse"
    # Waive the newest red gate.
    target = rp.newest_red_gate(records, [])
    assert target is not None
    idx, rec = target
    rp.record_waiver(waivers_path, rec["phase"], idx, "accepted for run 1")
    waivers = rp.load_jsonl(waivers_path)
    assert waivers == [{"phase": "P4", "record_index": 1, "reason": "accepted for run 1"}]
    # After waiver: P4 is waived, next advances to P5.
    states = rp.phase_states(PHASES, records, waivers)
    assert states[1] == ("P4", "waived")
    assert rp.next_action(states)[:2] == ("run", "P5")


def test_newest_red_gate_skips_already_waived() -> None:
    records = [_rec("P4", False), _rec("P5", False)]
    # Waive the P5 record (index 1); newest unwaived red is now P4 (index 0).
    waivers = [{"phase": "P5", "record_index": 1, "reason": "x"}]
    idx, rec = rp.newest_red_gate(records, waivers)
    assert rec["phase"] == "P4" and idx == 0


# --------------------------------------------------------------- rerun re-evaluation

def test_rerun_appends_supersize_record_flips_state(tmp_path) -> None:
    """A rerun appends a NEW (higher-index) record; latest-by-phase makes the
    phase green without touching the old red record."""
    import json

    from agora.fleet.phase_gate import TaskGateOutcome, evaluate_phase_gate

    phases_path = tmp_path / "phases.jsonl"
    phases_path.write_text(json.dumps(_rec("P5", False, blockers=["T5.1"])) + "\n", encoding="utf-8")

    # Simulate the rerun producing a green gate and appending it.
    green = evaluate_phase_gate("P5", [TaskGateOutcome("T5.1", True, [("pytest", True)])])
    rp.append_phase_record(phases_path, green, run_id="r001")

    records = rp.load_jsonl(phases_path)
    assert len(records) == 2  # old red retained, new green appended
    states = rp.phase_states(PHASES, records, [])
    assert dict(states)["P5"] == "green"  # latest record wins


# --------------------------------------------------------------- never reorder

def test_never_reorders_phases_regardless_of_write_order() -> None:
    # Records written out of flow order.
    scrambled = [_rec("P7", True), _rec("P3", True), _rec("P5", True), _rec("P4", True)]
    states = rp.phase_states(PHASES, scrambled, [])
    assert [p for p, _ in states] == PHASES  # declared order preserved
    # P3, P4, P5 green; P6 is the first pending → next runs P6, not P7.
    assert rp.next_action(states)[:2] == ("run", "P6")


# --------------------------------------------------------------- gate building + report

def test_outcomes_from_results_reads_blocking_from_flow() -> None:
    from types import SimpleNamespace

    tasks_by_id = {
        "T4.1": SimpleNamespace(blocking=True),
        "V4.1": SimpleNamespace(blocking=False),
    }
    results = [
        SimpleNamespace(task_id="T4.1", postcondition_results=[("impl", True, "")]),
        SimpleNamespace(task_id="V4.1", postcondition_results=[("parses", False, "bad")]),
    ]
    outs = rp.outcomes_from_results(tasks_by_id, results)
    by = {o.task_id: o for o in outs}
    assert by["T4.1"].blocking is True and by["T4.1"].passed is True
    assert by["V4.1"].blocking is False and by["V4.1"].passed is False


def test_gate_report_includes_runcheck_tails_and_nudges() -> None:
    from types import SimpleNamespace

    from agora.fleet.phase_gate import TaskGateOutcome, evaluate_phase_gate

    gate = evaluate_phase_gate("P5", [TaskGateOutcome("T5.1", True, [("pytest", False)])])
    result = SimpleNamespace(
        task_id="T5.1", nudges_used=2,
        run_check_records=[{
            "cmd": ["python", "-m", "pytest", "-q"], "exit_code": 1, "timed_out": False,
            "stdout": "1 failed", "stderr": "E   assert", "stdout_truncated": True,
            "stderr_truncated": False, "passed": False,
        }],
    )
    report = rp.format_gate_report(gate, {"T5.1": result}, {"nudge_budget": 1})
    assert "phase P5 gate: RED" in report
    assert "nudge accounting: 2 fired" in report
    assert "[stdout truncated]" in report
    assert "1 failed" in report


def test_report_is_ascii_safe() -> None:
    from types import SimpleNamespace

    from agora.fleet.phase_gate import TaskGateOutcome, evaluate_phase_gate

    gate = evaluate_phase_gate("P3", [TaskGateOutcome("T3.1", True, [("x", True)])])
    report = rp.format_gate_report(gate, {"T3.1": SimpleNamespace(nudges_used=0, run_check_records=[])}, {})
    report.encode("ascii")  # must not raise


# --------------------------------------------------------------- ollama health

def test_ollama_missing_models() -> None:
    tags = {"models": [{"name": "gemma4:e4b"}, {"name": "nomic-embed-text:latest"}]}
    assert rp.ollama_missing_models(tags, ["gemma4:e4b"]) == []
    assert rp.ollama_missing_models(tags, ["gemma4:e4b", "qwen2.5:7b-instruct"]) == ["qwen2.5:7b-instruct"]


# --------------------------------------------------------------- cast/agent resolution

def test_resolve_agent_models_maps_roles_to_cast() -> None:
    from agora.core.flow import instantiate_flow, load_flow
    from agora.fleet.cast import load_cast
    from agora.fleet.profiles import load_profiles

    flow = load_flow("flows/integration-run-1-echobot.flow.yaml")
    agents, _ = instantiate_flow(flow, "echobot", id_strategy="preserve")
    resolved, m2p = rp.resolve_agent_models(agents, load_cast("casts/p40-24gb.yaml"), load_profiles("profiles.yaml"))
    by = {a.name: a.model for a in resolved}
    assert by["impl"] == "ollama/gemma4:e4b"      # implementer -> cast implementer
    assert by["verifier"] == "ollama/qwen2.5:7b-instruct"  # reviewer -> cast verifier
    assert set(m2p) == {"ollama/gemma4:e4b", "ollama/qwen2.5:7b-instruct"}


def test_strip_cross_phase_deps() -> None:
    from agora.core.contract import Specification
    from agora.core.task import Task

    t = Task(id="T5.1", spec=Specification(), depends_on=("T4.2", "T5.0"))
    kept = rp.strip_cross_phase_deps({"T5.1", "T5.0"}, [t])[0]
    assert kept.depends_on == ("T5.0",)  # T4.2 (cross-phase) dropped


def test_repair_description_carries_oracle_verbatim() -> None:
    oracle = [{"cmd": ["python", "-m", "pytest", "-q"], "exit_code": 1, "timed_out": False,
               "stdout": "E   assert 1 == 2", "stderr": "", "stdout_truncated": False}]
    prompt = rp.build_repair_description("Write tests/test_core.py", oracle)
    assert "Write tests/test_core.py" in prompt
    assert "The following gate failed." in prompt
    assert "E   assert 1 == 2" in prompt  # oracle verbatim
