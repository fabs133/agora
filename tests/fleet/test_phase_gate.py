"""Phase grouping + gate evaluation (integration run 1)."""

from __future__ import annotations

from agora.core.flow import instantiate_flow, load_flow
from agora.fleet.phase_gate import (
    TaskGateOutcome,
    evaluate_phase_gate,
    first_red_phase,
    group_tasks_by_phase,
    ordered_phases,
    phases_runnable_through,
)


def _o(task_id, blocking, *pcs):
    return TaskGateOutcome(task_id, blocking, list(pcs))


def test_green_phase_all_blocking_pass() -> None:
    g = evaluate_phase_gate("P3", [_o("T3.1", True, ("files", True), ("import", True))])
    assert g.passed is True
    assert g.blockers == ()


def test_red_phase_blocking_task_fails() -> None:
    g = evaluate_phase_gate(
        "P5",
        [_o("T5.1", True, ("collect", True), ("pytest", False))],
    )
    assert g.passed is False
    assert g.blockers == ("T5.1",)


def test_nonblocking_task_never_gates() -> None:
    """A failed verifier (non-blocking) does not red the phase."""
    g = evaluate_phase_gate(
        "P4",
        [
            _o("T4.1", True, ("impl", True)),
            _o("V4.1", False, ("parses", False)),  # verifier failed
        ],
    )
    assert g.passed is True
    assert g.blockers == ()
    # The verifier outcome is still recorded for provenance.
    assert [t.task_id for t in g.nonblocking_tasks] == ["V4.1"]


def test_red_gate_blocks_next_phase() -> None:
    plan = ["P3", "P4", "P5", "P6"]
    results = [
        evaluate_phase_gate("P3", [_o("T3.1", True, ("x", True))]),
        evaluate_phase_gate("P4", [_o("T4.1", True, ("x", False))]),  # RED
    ]
    assert first_red_phase(results) == "P4"
    # P4 is red → the runner may run through P4 (where repair sits) but NOT P5/P6.
    assert phases_runnable_through(results, plan) == ["P3", "P4"]


def test_all_green_allows_full_plan() -> None:
    plan = ["P3", "P4"]
    results = [
        evaluate_phase_gate("P3", [_o("T3.1", True, ("x", True))]),
        evaluate_phase_gate("P4", [_o("T4.1", True, ("x", True))]),
    ]
    assert first_red_phase(results) is None
    assert phases_runnable_through(results, plan) == ["P3", "P4"]


def test_vacuous_pass_when_no_postconditions() -> None:
    g = evaluate_phase_gate("P7", [_o("T7.1", True)])  # no postconditions
    assert g.passed is True


def test_echobot_flow_phase_structure() -> None:
    """The real run-1 flow groups into the spec's phases; verifiers non-blocking."""
    flow = load_flow("flows/integration-run-1-echobot.flow.yaml")
    _, tasks = instantiate_flow(flow, "echobot", id_strategy="preserve")
    assert ordered_phases(tasks) == ["P3", "P4", "P5", "P6", "P7", "P9"]
    groups = group_tasks_by_phase(tasks)
    # Every phase has exactly one non-blocking verifier task.
    for phase, ts in groups.items():
        nonblocking = [t for t in ts if not t.blocking]
        assert len(nonblocking) == 1, f"{phase} should have one verifier"
        assert nonblocking[0].id.startswith("V")
