"""Task model plus DAG construction, topological sort, and status transitions.

Position: the unit the orchestrator schedules. A flow instantiates :class:`Task`
nodes; :func:`build_task_graph` / :func:`topological_sort` order them by their
``depends_on`` edges; the runtime walks that order, evaluates each task's
:class:`~agora.core.contract.Specification`, and drives status through the
machine in ``VALID_TASK_TRANSITIONS``.

Invariants:
  - Tasks are FROZEN — every mutation returns a fresh instance via
    :func:`dataclasses.replace`; nothing edits a task in place.
  - Status only moves along ``VALID_TASK_TRANSITIONS`` edges (enforced by
    :func:`transition_task`, which raises rather than silently allow a jump).
  - Two distinct edge kinds (F5): ``depends_on`` gates readiness on the
    predecessor reaching DONE; ``order_after`` gates only on a TERMINAL state
    (DONE or FAILED), so a verifier ordered after a blocking task still runs at
    gate time when that task FAILED — the fix for verifiers being skipped
    behind a red task.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace

from agora.core.contract import Specification
from agora.core.errors import AgoraError, InvalidTransition
from agora.core.types import AgentId, TaskId, TaskStatus

VALID_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.ASSIGNED, TaskStatus.FAILED}),
    TaskStatus.ASSIGNED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.PENDING, TaskStatus.FAILED}
    ),
    TaskStatus.RUNNING: frozenset({TaskStatus.REVIEW, TaskStatus.FAILED}),
    TaskStatus.REVIEW: frozenset(
        {TaskStatus.DONE, TaskStatus.RUNNING, TaskStatus.FAILED}
    ),
    TaskStatus.DONE: frozenset(),
    TaskStatus.FAILED: frozenset({TaskStatus.PENDING}),
}


@dataclass(frozen=True)
class Task:
    """One unit of work in a project DAG.

    Each task carries its own :class:`~agora.core.contract.Specification`
    (preconditions + postconditions evaluated by the orchestrator) and a
    ``depends_on`` tuple that defines the DAG edges. Status transitions go
    through :func:`transition_task` to enforce the state machine in
    ``VALID_TASK_TRANSITIONS``.

    Tasks are immutable; mutations return a new instance via
    :func:`dataclasses.replace`. ``artifacts`` and ``result_summary`` are
    populated when the runtime evaluates the postconditions and the agent
    completes its turn.
    """

    id: TaskId
    spec: Specification
    description: str = ""
    agent_id: AgentId | None = None
    depends_on: tuple[TaskId, ...] = ()
    status: TaskStatus = TaskStatus.PENDING
    artifacts: tuple[str, ...] = ()
    result_summary: str = ""
    created_at: str = ""
    updated_at: str = ""
    #: Required relative path of the task's primary output file. When set,
    #: agent prompts display it as a structured constant and ``write_file``
    #: logs a warning if called with a different path. Empty string means
    #: "no single canonical output".
    output_path: str = ""
    #: Integration run 1: the phase this task belongs to (e.g. ``"P3"``). The
    #: phase-staged runner groups tasks by this and gates at each boundary.
    #: Empty string means "no phase" (pre-run-1 flows).
    phase: str = ""
    #: Integration run 1: whether this task's postconditions gate its phase.
    #: Verifier tasks are ``blocking=False`` — their verdict is recorded but
    #: never blocks the next phase.
    blocking: bool = True
    #: Integration run 1.2 (F5 fix): ORDERING-only predecessors. Unlike
    #: ``depends_on`` (which gates readiness on the predecessor reaching DONE),
    #: an ``order_after`` task runs once its predecessors reach a TERMINAL state
    #: (DONE **or** FAILED) — so a verifier ordered after a blocking task runs
    #: unconditionally at gate time, even when that task failed.
    order_after: tuple[TaskId, ...] = ()


def transition_task(task: Task, new_status: TaskStatus) -> Task:
    """Validate and apply a status change. Returns a new Task."""
    allowed = VALID_TASK_TRANSITIONS[task.status]
    if new_status not in allowed:
        raise InvalidTransition(
            from_state=task.status.value,
            to_state=new_status.value,
            reason=f"task {task.id}: allowed next states are {sorted(s.value for s in allowed)}",
        )
    return replace(task, status=new_status)


def build_dag(tasks: list[Task]) -> dict[TaskId, list[TaskId]]:
    """Build adjacency list (task_id -> dependency ids). Raises on cycles or missing deps."""
    by_id = {t.id: t for t in tasks}
    adjacency: dict[TaskId, list[TaskId]] = {}
    for t in tasks:
        for dep in t.depends_on:
            if dep not in by_id:
                raise AgoraError(f"task {t.id} depends on unknown task {dep}")
        for pred in t.order_after:
            if pred not in by_id:
                raise AgoraError(f"task {t.id} order_after unknown task {pred}")
        # order_after edges are ordering constraints too — include them so cycle
        # detection and topological_sort treat them like depends_on for SEQUENCE
        # (readiness/gating semantics differ; see ready_tasks).
        adjacency[t.id] = list(t.depends_on) + [
            p for p in t.order_after if p not in t.depends_on
        ]
    # Cycle detection via DFS with coloring.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[TaskId, int] = {tid: WHITE for tid in adjacency}

    def visit(node: TaskId) -> None:
        color[node] = GRAY
        for dep in adjacency[node]:
            if color[dep] == GRAY:
                raise AgoraError(f"cycle detected involving task {dep}")
            if color[dep] == WHITE:
                visit(dep)
        color[node] = BLACK

    for tid in adjacency:
        if color[tid] == WHITE:
            visit(tid)
    return adjacency


def topological_sort(tasks: list[Task]) -> list[Task]:
    """Return tasks in dependency-respecting order (Kahn's algorithm)."""
    adjacency = build_dag(tasks)
    by_id = {t.id: t for t in tasks}

    # Reverse edges: dep -> dependents.
    dependents: dict[TaskId, list[TaskId]] = {tid: [] for tid in adjacency}
    indegree: dict[TaskId, int] = {tid: 0 for tid in adjacency}
    for tid, deps in adjacency.items():
        indegree[tid] = len(deps)
        for dep in deps:
            dependents[dep].append(tid)

    queue = deque(sorted(tid for tid, deg in indegree.items() if deg == 0))
    ordered: list[Task] = []
    while queue:
        tid = queue.popleft()
        ordered.append(by_id[tid])
        for nxt in sorted(dependents[tid]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if len(ordered) != len(tasks):
        raise AgoraError("topological sort failed (cycle detected)")
    return ordered


#: A task ordered after another may run once the predecessor is TERMINAL —
#: succeeded or failed. (depends_on requires DONE; order_after requires only
#: that the predecessor has finished running.)
_TERMINAL_STATUSES = frozenset({TaskStatus.DONE, TaskStatus.FAILED})


def ready_tasks(tasks: list[Task]) -> list[Task]:
    """Return PENDING tasks whose ``depends_on`` are all DONE **and** whose
    ``order_after`` predecessors have all reached a terminal state.

    ``depends_on`` gates on success (predecessor DONE); ``order_after`` gates
    only on completion (DONE or FAILED) — so a verifier ordered after a blocking
    task runs even when that task failed (F5 fix).
    """
    by_id = {t.id: t for t in tasks}
    ready: list[Task] = []
    for t in tasks:
        if t.status != TaskStatus.PENDING:
            continue
        deps_done = all(
            by_id[dep].status == TaskStatus.DONE for dep in t.depends_on if dep in by_id
        )
        order_done = all(
            by_id[pred].status in _TERMINAL_STATUSES
            for pred in t.order_after if pred in by_id
        )
        if deps_done and order_done:
            ready.append(t)
    return ready
