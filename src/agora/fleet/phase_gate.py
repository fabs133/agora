"""Phase grouping + gate evaluation for the phase-staged runner (integration run 1).

A run-1 flow tags each task with a ``phase`` (e.g. ``"P3"``). The runner executes
phases in DECLARED order and pauses at each boundary (the staged-pause discipline
from ``scripts/run_sweep_staged.py``, applied to phases within one run). A phase's
GATE is green iff every BLOCKING task in the phase had all its postconditions
pass; non-blocking tasks (verifiers) are recorded but never gate. A red gate
stops the run before the next phase — repair happens against that boundary.

This module is pure: it turns ``(tasks, per-task postcondition results)`` into an
ordered list of :class:`PhaseGateResult`. The live runner supplies the results
(from postcondition evaluation) and consults :func:`first_red_phase` to decide
whether to advance. Provenance is emitted via :class:`PhaseGateRecord`
(see :mod:`agora.observe.jsonl`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# One task's postcondition outcomes: (predicate_name, passed) pairs.
PostconditionOutcomes = list[tuple[str, bool]]


@dataclass(frozen=True)
class TaskGateOutcome:
    """One task's contribution to its phase gate."""

    task_id: str
    blocking: bool
    postconditions: PostconditionOutcomes = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """All postconditions green (vacuously True when the task has none)."""
        return all(p for _, p in self.postconditions)


@dataclass(frozen=True)
class PhaseGateResult:
    """The gate outcome for one phase.

    ``passed`` is True iff every BLOCKING task passed. ``blockers`` names the
    blocking tasks that failed (empty when green). Non-blocking task outcomes
    are retained in ``tasks`` for provenance but excluded from the verdict.
    """

    phase: str
    tasks: tuple[TaskGateOutcome, ...]
    passed: bool
    blockers: tuple[str, ...]
    #: True when produced by a mechanical re-evaluation over the workspace
    #: (cross-phase repair) rather than a live task run — read as an
    #: ARTIFACT-STATE check, its genuine signal being run_check re-execution.
    mechanical: bool = False

    @property
    def blocking_tasks(self) -> tuple[TaskGateOutcome, ...]:
        return tuple(t for t in self.tasks if t.blocking)

    @property
    def nonblocking_tasks(self) -> tuple[TaskGateOutcome, ...]:
        return tuple(t for t in self.tasks if not t.blocking)


def ordered_phases(tasks) -> list[str]:
    """Phases in first-seen (declared) order. Tasks with an empty phase are
    skipped — a flow that tags no task with a phase has no phase plan."""
    seen: list[str] = []
    for t in tasks:
        ph = getattr(t, "phase", "") or ""
        if ph and ph not in seen:
            seen.append(ph)
    return seen


def group_tasks_by_phase(tasks) -> dict[str, list]:
    """Map phase → tasks (declared order preserved within each phase)."""
    groups: dict[str, list] = {}
    for t in tasks:
        ph = getattr(t, "phase", "") or ""
        if not ph:
            continue
        groups.setdefault(ph, []).append(t)
    return groups


def evaluate_phase_gate(
    phase: str,
    outcomes: list[TaskGateOutcome],
) -> PhaseGateResult:
    """Compute one phase's gate from its tasks' postcondition outcomes.

    Green iff every BLOCKING task passed. Non-blocking tasks never gate.
    """
    blockers = tuple(o.task_id for o in outcomes if o.blocking and not o.passed)
    return PhaseGateResult(
        phase=phase,
        tasks=tuple(outcomes),
        passed=not blockers,
        blockers=blockers,
    )


def first_red_phase(results: list[PhaseGateResult]) -> str | None:
    """Return the phase name of the first red gate in order, else None.

    The staged runner stops before the phase AFTER this one — a red gate blocks
    every downstream phase until repair turns it green.
    """
    for r in results:
        if not r.passed:
            return r.phase
    return None


def phases_runnable_through(results: list[PhaseGateResult], plan: list[str]) -> list[str]:
    """Given results so far and the full declared phase ``plan``, return the
    phases that may run: every phase up to and including the first red one
    (that red phase is where work/repair sits), and no further.
    """
    runnable: list[str] = []
    done = {r.phase: r for r in results}
    for phase in plan:
        runnable.append(phase)
        r = done.get(phase)
        if r is not None and not r.passed:
            break  # red gate — do not open the next phase
    return runnable


__all__ = [
    "PhaseGateResult",
    "TaskGateOutcome",
    "evaluate_phase_gate",
    "first_red_phase",
    "group_tasks_by_phase",
    "ordered_phases",
    "phases_runnable_through",
]
