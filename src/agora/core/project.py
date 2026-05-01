"""Project state machine with the approve/loop-back gate."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import NamedTuple

from agora.core.errors import InvalidTransition
from agora.core.task import Task
from agora.core.types import AgentId, ProjectId, ProjectPhase, TaskStatus


class PhaseTransition(NamedTuple):
    """One edge in the project phase state machine.

    The ``condition`` string is human-readable rationale shown in the
    observer surface; it does not gate the transition. Validity is determined
    by membership in :data:`VALID_TRANSITIONS`.
    """

    from_phase: ProjectPhase
    to_phase: ProjectPhase
    condition: str


@dataclass(frozen=True)
class PhaseChange:
    """A single phase transition recorded on a :class:`Project`.

    Appended to ``Project.phase_history`` by :func:`transition_phase` with a
    UTC ISO timestamp so the run timeline is reconstructable from the project
    state alone.
    """

    from_phase: ProjectPhase
    to_phase: ProjectPhase
    reason: str
    timestamp: str


def _base_transitions() -> list[PhaseTransition]:
    T = PhaseTransition
    P = ProjectPhase
    transitions = [
        T(P.INIT, P.ANALYSIS, "Project spec loaded and agents spawned"),
        T(P.ANALYSIS, P.ARCHITECTURE, "Requirements analyzed, tasks decomposed"),
        T(P.ARCHITECTURE, P.IMPLEMENTATION, "Architecture approved, contracts defined"),
        T(P.IMPLEMENTATION, P.TESTING, "All implementation tasks complete"),
        T(P.TESTING, P.REVIEW, "Tests pass, coverage meets threshold"),
        T(P.REVIEW, P.DONE, "Human or reviewer approves"),
        T(P.REVIEW, P.ANALYSIS, "Review rejects: requirements unclear"),
        T(P.REVIEW, P.ARCHITECTURE, "Review rejects: design issues"),
        T(P.REVIEW, P.IMPLEMENTATION, "Review rejects: implementation issues"),
        T(P.TESTING, P.IMPLEMENTATION, "Tests fail: implementation issues"),
    ]
    # Any non-terminal/non-failed phase may transition to FAILED.
    for phase in ProjectPhase:
        if phase not in (P.FAILED, P.DONE):
            transitions.append(T(phase, P.FAILED, "Unrecoverable error"))
    return transitions


VALID_TRANSITIONS: list[PhaseTransition] = _base_transitions()
_ALLOWED: dict[ProjectPhase, frozenset[ProjectPhase]] = {}
for _t in VALID_TRANSITIONS:
    _ALLOWED.setdefault(_t.from_phase, set()).add(_t.to_phase)  # type: ignore[arg-type]
_ALLOWED = {k: frozenset(v) for k, v in _ALLOWED.items()}


@dataclass(frozen=True)
class Project:
    """A run-in-progress: agents, the task DAG, the phase state machine.

    Projects are immutable; mutations return a new instance via
    :func:`dataclasses.replace`. ``phase`` advances through the
    :class:`~agora.core.types.ProjectPhase` state machine via
    :func:`transition_phase`, with each transition appended to
    ``phase_history`` for audit. ``git_repo_path`` is the per-project work
    tree where the orchestrator writes artefacts and runs auto-commits.
    """

    id: ProjectId
    name: str
    phase: ProjectPhase = ProjectPhase.INIT
    agents: tuple[AgentId, ...] = ()
    tasks: tuple[Task, ...] = ()
    phase_history: tuple[PhaseChange, ...] = ()
    git_repo_path: str = ""
    created_at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def transition_phase(project: Project, new_phase: ProjectPhase, reason: str) -> Project:
    """Validate and apply a phase transition. Returns a new Project."""
    allowed = _ALLOWED.get(project.phase, frozenset())
    if new_phase not in allowed:
        raise InvalidTransition(
            from_state=project.phase.value,
            to_state=new_phase.value,
            reason=f"project {project.id}: allowed next phases are {sorted(p.value for p in allowed)}",
        )
    change = PhaseChange(
        from_phase=project.phase,
        to_phase=new_phase,
        reason=reason,
        timestamp=_now_iso(),
    )
    return replace(
        project,
        phase=new_phase,
        phase_history=project.phase_history + (change,),
    )


_PHASE_TASK_FILTER: dict[ProjectPhase, frozenset[TaskStatus]] = {
    ProjectPhase.IMPLEMENTATION: frozenset(
        {TaskStatus.PENDING, TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.REVIEW}
    ),
    ProjectPhase.TESTING: frozenset(
        {TaskStatus.PENDING, TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.REVIEW}
    ),
}


def current_phase_tasks(project: Project) -> list[Task]:
    """Tasks relevant to the current phase (active / not done)."""
    active = _PHASE_TASK_FILTER.get(
        project.phase,
        frozenset({TaskStatus.PENDING, TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.REVIEW}),
    )
    return [t for t in project.tasks if t.status in active]


def can_advance(project: Project) -> tuple[bool, str]:
    """Whether all tasks have reached a terminal state (DONE)."""
    if not project.tasks:
        return True, ""
    pending = [t for t in project.tasks if t.status != TaskStatus.DONE]
    if pending:
        return False, f"{len(pending)} task(s) not yet DONE"
    return True, ""
