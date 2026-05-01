"""Task model plus DAG construction, topological sort, and status transitions."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace

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
        adjacency[t.id] = list(t.depends_on)
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


def ready_tasks(tasks: list[Task]) -> list[Task]:
    """Return PENDING tasks whose dependencies are all DONE."""
    by_id = {t.id: t for t in tasks}
    ready: list[Task] = []
    for t in tasks:
        if t.status != TaskStatus.PENDING:
            continue
        if all(by_id[dep].status == TaskStatus.DONE for dep in t.depends_on if dep in by_id):
            ready.append(t)
    return ready
