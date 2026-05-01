import pytest

from agora.core.contract import Specification
from agora.core.errors import InvalidTransition
from agora.core.project import Project, can_advance, transition_phase
from agora.core.task import Task
from agora.core.types import ProjectPhase, TaskStatus


def _project(phase: ProjectPhase = ProjectPhase.INIT, tasks: tuple[Task, ...] = ()) -> Project:
    return Project(id="proj-1", name="test", phase=phase, tasks=tasks)


def test_valid_phase_transition_init_to_analysis() -> None:
    p = _project()
    out = transition_phase(p, ProjectPhase.ANALYSIS, "spec loaded")
    assert out.phase == ProjectPhase.ANALYSIS
    assert p.phase == ProjectPhase.INIT


def test_invalid_phase_transition_init_to_done() -> None:
    p = _project()
    with pytest.raises(InvalidTransition):
        transition_phase(p, ProjectPhase.DONE, "skip")


def test_review_to_analysis_loopback() -> None:
    p = _project(phase=ProjectPhase.REVIEW)
    out = transition_phase(p, ProjectPhase.ANALYSIS, "reviewer wants to re-scope")
    assert out.phase == ProjectPhase.ANALYSIS


def test_review_to_implementation_loopback() -> None:
    p = _project(phase=ProjectPhase.REVIEW)
    out = transition_phase(p, ProjectPhase.IMPLEMENTATION, "implementation issues")
    assert out.phase == ProjectPhase.IMPLEMENTATION


def test_testing_to_implementation_loopback() -> None:
    p = _project(phase=ProjectPhase.TESTING)
    out = transition_phase(p, ProjectPhase.IMPLEMENTATION, "tests failed")
    assert out.phase == ProjectPhase.IMPLEMENTATION


def test_phase_history_recorded() -> None:
    p = _project()
    out = transition_phase(p, ProjectPhase.ANALYSIS, "why")
    assert len(out.phase_history) == 1
    ch = out.phase_history[0]
    assert ch.from_phase == ProjectPhase.INIT
    assert ch.to_phase == ProjectPhase.ANALYSIS
    assert ch.reason == "why"
    assert ch.timestamp


def test_can_advance_all_tasks_done() -> None:
    task = Task(id="t1", spec=Specification(), status=TaskStatus.DONE)
    p = _project(phase=ProjectPhase.IMPLEMENTATION, tasks=(task,))
    ok, reason = can_advance(p)
    assert ok is True
    assert reason == ""


def test_can_advance_tasks_pending() -> None:
    task = Task(id="t1", spec=Specification(), status=TaskStatus.PENDING)
    p = _project(phase=ProjectPhase.IMPLEMENTATION, tasks=(task,))
    ok, reason = can_advance(p)
    assert ok is False
    assert "not yet DONE" in reason
