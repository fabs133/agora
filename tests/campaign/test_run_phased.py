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


# --------------------------------------------------------------- persistence + oracle

def test_build_and_append_task_record_round_trips(tmp_path) -> None:
    from types import SimpleNamespace

    from agora.observe.jsonl import TaskRecord

    task = SimpleNamespace(id="T5.1", output_path="tests/test_core.py",
                           spec=SimpleNamespace(postconditions=[]), stages=[],
                           agent_id="impl", phase="P5", blocking=True)
    result = SimpleNamespace(
        success=False, output="", iterations=3,
        postcondition_results=[("run_check_pytest", False, "1 failed")],
        run_check_records=[{"cmd": ["python", "-m", "pytest"], "exit_code": 1,
                            "timed_out": False, "stdout": "boom", "stderr": "",
                            "stdout_truncated": False, "stderr_truncated": False, "passed": False}],
        nudges_used=1, reviews_used=0, post_review_action=None, tools_used=["write_file"],
    )
    rec = rp.build_task_record(task, result, "implementer", 0, "r001")
    assert rec.phase == "P5" and rec.blocking is True and rec.nudges_used == 1
    assert rec.run_check_records[0]["stdout"] == "boom"
    rp.append_task_records(tmp_path / "tasks.jsonl", [rec])
    parsed = TaskRecord.model_validate(
        __import__("json").loads((tmp_path / "tasks.jsonl").read_text(encoding="utf-8").splitlines()[0])
    )
    assert parsed.task_id == "T5.1" and parsed.run_check_records[0]["stdout"] == "boom"


def test_cross_invocation_oracle_carries_stdout_verbatim(tmp_path) -> None:
    """Invocation 1 persists a red phase with a distinctive run_check stdout;
    invocation 2's rerun oracle → repair prompt contains that string verbatim
    (with its truncation flag), sourced from the PERSISTED tasks.jsonl."""
    from agora.observe.jsonl import TaskRecord

    marker = "AssertionError: pong != PONG  <<distinctive-oracle-marker-42>>"

    # --- invocation 1: write a red P5 TaskRecord (as run_phase would) ---
    tr = TaskRecord(
        run_id="r001", task_id="T5.1", task_index=0, role="implementer",
        task_kind="test_authoring", status="failed", first_pass=False,
        loopback_count=0, iterations=3, phase="P5", blocking=True,
        postconditions=[{"name": "run_check_pytest_ab12", "passed": False}],
        run_check_records=[{
            "cmd": ["python", "-m", "pytest", "-q"], "exit_code": 1, "timed_out": False,
            "stdout": marker, "stderr": "", "stdout_truncated": True,
            "stderr_truncated": False, "passed": False,
        }],
    )
    (tmp_path / "tasks.jsonl").write_text(tr.model_dump_json() + "\n", encoding="utf-8")

    # --- invocation 2: resolve oracle from persisted records, build repair prompt ---
    task_records = rp.load_jsonl(tmp_path / "tasks.jsonl")
    oracle = rp.oracle_records_for_phase(task_records, "P5")
    prompt = rp.build_repair_description("Write tests/test_core.py", oracle)

    assert marker in prompt                # stdout carried VERBATIM across invocations
    assert "stdout [truncated]" in prompt  # truncation flag carried
    assert "The following gate failed." in prompt


def test_oracle_skips_passed_and_nonblocking_tasks(tmp_path) -> None:
    from agora.observe.jsonl import TaskRecord

    def _rec_row(tid, status, blocking, stdout):
        return TaskRecord(
            run_id="r", task_id=tid, task_index=0, role="impl", task_kind="code_body",
            status=status, first_pass=(status == "passed"), loopback_count=0, iterations=1,
            phase="P4", blocking=blocking,
            postconditions=[{"name": "run_check_x", "passed": status == "passed"}],
            run_check_records=[{"cmd": ["c"], "exit_code": 0 if status == "passed" else 1,
                                "timed_out": False, "stdout": stdout, "stderr": "",
                                "stdout_truncated": False, "stderr_truncated": False,
                                "passed": status == "passed"}],
        ).model_dump_json()

    (tmp_path / "tasks.jsonl").write_text(
        "\n".join([
            _rec_row("T4.1", "passed", True, "GREEN_OK"),
            _rec_row("V4.1", "failed", False, "VERIFIER_FAIL"),  # non-blocking
            _rec_row("T4.2", "failed", True, "REAL_ORACLE"),
        ]) + "\n",
        encoding="utf-8",
    )
    oracle = rp.oracle_records_for_phase(rp.load_jsonl(tmp_path / "tasks.jsonl"), "P4")
    blob = rp.build_repair_description("orig", oracle)
    assert "REAL_ORACLE" in blob          # failing blocking task's oracle included
    assert "GREEN_OK" not in blob         # passed task excluded
    assert "VERIFIER_FAIL" not in blob    # non-blocking task excluded


# --------------------------------------------------------------- run.log (item 1)

async def test_run_log_captures_tool_result(tmp_path, fake_matrix_client) -> None:
    """A fixture phase run produces run.log containing a per-turn tool-result
    string — the observer the phased runner was missing. Here an implementer
    write to tests/ is scope-rejected, and that rejection is now VISIBLE."""
    from agora.core.agent import AgentConfig, AgentIdentity
    from agora.core.contract import Specification
    from agora.core.task import Task
    from agora.core.types import AgentRole, TaskStatus
    from agora.fleet.agent_runtime import AgentRuntime
    from agora.fleet.inner_tools import ToolContext
    from agora.fleet.llm_adapter import LLMResponse
    from tests.conftest import FakeLLM, tool_call

    llm = FakeLLM([
        LLMResponse(content="", tool_calls=(tool_call("write_file", {"path": "tests/x.py", "content": "x"}),)),
        LLMResponse(content="done", tool_calls=()),
        LLMResponse(content="[]"),
    ])
    await fake_matrix_client.create_room(name="agent", topic="")
    room = next(iter(fake_matrix_client.rooms))
    proj = await fake_matrix_client.create_room(name="proj", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path), matrix_client=fake_matrix_client,
        agent_room_id=room, project_room_id=proj, tool_errors="corrective",
    )
    identity = AgentIdentity(
        agent_id="@i:x", room_id=room,
        config=AgentConfig(name="i", role=AgentRole.IMPLEMENTER, instructions="do"),
    )
    runtime = AgentRuntime(llm=llm, matrix_client=fake_matrix_client, tool_context=ctx)

    log_path = tmp_path / "run.log"
    state = rp.attach_run_log(log_path, "P5")
    try:
        await runtime.execute_task(
            Task(id="t", spec=Specification(), description="x", status=TaskStatus.PENDING),
            identity,
        )
    finally:
        rp.detach_run_log(state)

    text = log_path.read_text(encoding="utf-8")
    assert "tool call:" in text          # per-turn tool result recorded
    assert "write_file" in text
    assert "rejected" in text            # the scope rejection is now visible in run.log


def test_detach_run_log_restores_logger(tmp_path) -> None:
    import logging as _logging

    lg = _logging.getLogger("agora")
    before = len(lg.handlers)
    state = rp.attach_run_log(tmp_path / "run.log", "P3")
    assert len(lg.handlers) == before + 1
    rp.detach_run_log(state)
    assert len(lg.handlers) == before  # handler removed, no leak


# --------------------------------------------------------------- cross-phase rerun (item 4)

def test_reevaluate_phase_gate_over_workspace(tmp_path) -> None:
    """Gate of phase X re-evaluated mechanically over the workspace (no LLM):
    green when the artifact satisfies it, red when it does not."""
    from agora.core.contract import Specification
    from agora.core.task import Task
    from agora.plan.predicate_registry import build_predicate

    (tmp_path / "src.txt").write_text("contains the MARKER token", encoding="utf-8")
    spec = Specification(postconditions=(
        build_predicate("file_contains", {"rel": "src.txt", "substring": "MARKER"}),
    ))
    task = Task(id="TX", spec=spec, phase="P5", blocking=True)

    gate, results = rp.reevaluate_phase_gate(tmp_path, "P5", [task])
    assert gate.passed is True
    assert "TX" in results

    (tmp_path / "src.txt").write_text("marker removed", encoding="utf-8")
    gate2, _ = rp.reevaluate_phase_gate(tmp_path, "P5", [task])
    assert gate2.passed is False
    assert gate2.blockers == ("TX",)


def test_mechanical_flag_round_trips(tmp_path) -> None:
    """reevaluate_phase_gate marks its gate mechanical; the flag survives to the
    PhaseGateRecord in phases.jsonl and is distinguishable in --status."""
    from agora.core.contract import Specification
    from agora.core.task import Task
    from agora.observe.jsonl import PhaseGateRecord
    from agora.plan.predicate_registry import build_predicate

    (tmp_path / "src.txt").write_text("has MARKER", encoding="utf-8")
    spec = Specification(postconditions=(
        build_predicate("file_contains", {"rel": "src.txt", "substring": "MARKER"}),
    ))
    gate, _ = rp.reevaluate_phase_gate(
        tmp_path, "P5", [Task(id="TX", spec=spec, phase="P5", blocking=True)]
    )
    assert gate.mechanical is True

    phases_path = tmp_path / "phases.jsonl"
    rp.append_phase_record(phases_path, gate, run_id="r001")
    parsed = PhaseGateRecord.model_validate(rp.load_jsonl(phases_path)[0])
    assert parsed.mechanical is True

    # A live (non-mechanical) gate defaults False and stays False through append.
    from agora.fleet.phase_gate import TaskGateOutcome, evaluate_phase_gate
    live = evaluate_phase_gate("P4", [TaskGateOutcome("T4.1", True, [("x", True)])])
    assert live.mechanical is False
    rp.append_phase_record(phases_path, live, run_id="r001")
    assert rp.load_jsonl(phases_path)[1]["mechanical"] is False


def test_cross_phase_oracle_templated_onto_other_phase_task(tmp_path) -> None:
    """--rerun-task Y --oracle X: oracle sourced from phase X (persisted records)
    is templated onto a task from a DIFFERENT phase Y (verbatim), and phase X's
    gate is what gets re-evaluated afterward."""
    from agora.observe.jsonl import TaskRecord

    marker = "E   assert handle_message signature mismatch <<xphase-marker>>"
    # Phase X = P5 has a red record whose oracle names the failure.
    tr = TaskRecord(
        run_id="r", task_id="T5.1", task_index=0, role="tester", task_kind="test_authoring",
        status="failed", first_pass=False, loopback_count=0, iterations=2, phase="P5", blocking=True,
        postconditions=[{"name": "run_check_pytest", "passed": False}],
        run_check_records=[{"cmd": ["python", "-m", "pytest", "-q"], "exit_code": 1,
                            "timed_out": False, "stdout": marker, "stderr": "",
                            "stdout_truncated": False, "stderr_truncated": False, "passed": False}],
    )
    (tmp_path / "tasks.jsonl").write_text(tr.model_dump_json() + "\n", encoding="utf-8")

    # Oracle from X=P5, templated onto Y=T4.1 (a P4 src task) — X's stdout verbatim.
    oracle = rp.oracle_records_for_phase(rp.load_jsonl(tmp_path / "tasks.jsonl"), "P5")
    prompt = rp.build_repair_description("Implement handle_message router in src", oracle)
    assert "Implement handle_message router in src" in prompt   # Y's original task
    assert marker in prompt                                     # X's oracle, verbatim
