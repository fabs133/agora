import pytest

from agora.core.contract import Specification
from agora.core.errors import AgoraError, InvalidTransition
from agora.core.task import (
    Task,
    build_dag,
    ready_tasks,
    topological_sort,
    transition_task,
)
from agora.core.types import TaskStatus


def _task(tid: str, deps: tuple[str, ...] = (), status: TaskStatus = TaskStatus.PENDING) -> Task:
    return Task(id=tid, spec=Specification(description=tid), depends_on=deps, status=status)


def test_valid_transition_pending_to_assigned() -> None:
    t = _task("1")
    out = transition_task(t, TaskStatus.ASSIGNED)
    assert out.status == TaskStatus.ASSIGNED
    assert t.status == TaskStatus.PENDING  # original unchanged


def test_invalid_transition_done_to_running() -> None:
    t = _task("1", status=TaskStatus.DONE)
    with pytest.raises(InvalidTransition):
        transition_task(t, TaskStatus.RUNNING)


def test_failed_to_pending_retry() -> None:
    t = _task("1", status=TaskStatus.FAILED)
    out = transition_task(t, TaskStatus.PENDING)
    assert out.status == TaskStatus.PENDING


def test_build_dag_no_cycles() -> None:
    tasks = [_task("a"), _task("b", deps=("a",)), _task("c", deps=("b",))]
    adj = build_dag(tasks)
    assert adj == {"a": [], "b": ["a"], "c": ["b"]}


def test_build_dag_detects_cycle() -> None:
    a = _task("a", deps=("b",))
    b = _task("b", deps=("a",))
    with pytest.raises(AgoraError, match="cycle"):
        build_dag([a, b])


def test_build_dag_missing_dep_raises() -> None:
    tasks = [_task("a", deps=("ghost",))]
    with pytest.raises(AgoraError, match="unknown task"):
        build_dag(tasks)


def test_topological_sort_linear() -> None:
    tasks = [_task("c", deps=("b",)), _task("b", deps=("a",)), _task("a")]
    ordered = [t.id for t in topological_sort(tasks)]
    assert ordered == ["a", "b", "c"]


def test_topological_sort_diamond() -> None:
    # a -> b, a -> c, b -> d, c -> d
    tasks = [
        _task("a"),
        _task("b", deps=("a",)),
        _task("c", deps=("a",)),
        _task("d", deps=("b", "c")),
    ]
    ordered = [t.id for t in topological_sort(tasks)]
    assert ordered[0] == "a"
    assert ordered[-1] == "d"
    assert set(ordered[1:3]) == {"b", "c"}


def test_ready_tasks_with_unmet_deps() -> None:
    a = _task("a", status=TaskStatus.DONE)
    b = _task("b", deps=("a",))  # ready (dep done)
    c = _task("c", deps=("b",))  # not ready (dep pending)
    out = {t.id for t in ready_tasks([a, b, c])}
    assert out == {"b"}


# --- order_after: ordering WITHOUT success-gating (F5 fix) ---

def _oa_task(tid, order_after=(), deps=(), status=TaskStatus.PENDING):
    return Task(id=tid, spec=Specification(description=tid), depends_on=deps,
                order_after=order_after, status=status)


def test_order_after_runs_after_FAILED_predecessor() -> None:
    """A verifier ordered after a failed blocking task still becomes ready —
    the F5 fix (depends_on would have gated it out)."""
    t = _oa_task("T5.1", status=TaskStatus.FAILED)
    v = _oa_task("V5.1", order_after=("T5.1",))
    assert {x.id for x in ready_tasks([t, v])} == {"V5.1"}


def test_order_after_respects_ordering_until_terminal() -> None:
    """order_after does NOT run while its predecessor is still PENDING/RUNNING."""
    t = _oa_task("T5.1", status=TaskStatus.PENDING)
    v = _oa_task("V5.1", order_after=("T5.1",))
    assert {x.id for x in ready_tasks([t, v])} == {"T5.1"}  # V waits
    t_run = _oa_task("T5.1", status=TaskStatus.RUNNING)
    assert {x.id for x in ready_tasks([t_run, v])} == set()  # neither ready


def test_order_after_runs_after_DONE_predecessor() -> None:
    t = _oa_task("T5.1", status=TaskStatus.DONE)
    v = _oa_task("V5.1", order_after=("T5.1",))
    assert {x.id for x in ready_tasks([t, v])} == {"V5.1"}


def test_depends_on_still_gates_on_success() -> None:
    """depends_on semantics unchanged: a FAILED dependency blocks readiness."""
    a = _oa_task("a", status=TaskStatus.FAILED)
    b = _oa_task("b", deps=("a",))
    assert {x.id for x in ready_tasks([a, b])} == set()  # b NOT ready (dep failed)


def test_build_dag_validates_order_after_ids() -> None:
    from agora.core.task import build_dag

    good = [_oa_task("a", status=TaskStatus.DONE), _oa_task("b", order_after=("a",))]
    build_dag(good)  # no raise
    with pytest.raises(AgoraError, match="order_after unknown"):
        build_dag([_oa_task("b", order_after=("missing",))])
