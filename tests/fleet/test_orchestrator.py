from pathlib import Path

from agora.core.agent import AgentConfig
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole, ProjectPhase, TaskStatus
from agora.fleet.llm_adapter import LLMResponse
from agora.fleet.orchestrator import Orchestrator, ReviewDecision
from agora.matrix.room_manager import RoomManager
from tests.conftest import FakeLLM, tool_call


def _always_pass_spec() -> Specification:
    return Specification(
        postconditions=(
            make_predicate("ok", "true", lambda _ctx: (True, "")),
        ),
        description="trivial",
    )


def _fail_spec() -> Specification:
    return Specification(
        postconditions=(
            make_predicate("never", "always fails", lambda _ctx: (False, "nope")),
        ),
        description="impossible",
    )


def _make_llm_for_simple_complete() -> FakeLLM:
    # Responses: tool_call mark_complete → stop → reflection empty.
    return FakeLLM(
        [
            LLMResponse(
                content="",
                tool_calls=(tool_call("mark_complete", {"summary": "done"}),),
            ),
            LLMResponse(content="complete"),
            LLMResponse(content="[]"),
        ]
        * 10  # enough replay for multiple tasks
    )


def _make_orchestrator(tmp_path: Path, fake_matrix_client, llm_plan_factory):
    mgr = RoomManager(fake_matrix_client, homeserver_name="agora.local")

    def factory(_model: str):
        return llm_plan_factory()

    return Orchestrator(
        matrix_client=fake_matrix_client,
        room_manager=mgr,
        llm_factory=factory,
        work_dir=str(tmp_path),
    )


async def test_single_task_mode(tmp_path, fake_matrix_client) -> None:
    orch = _make_orchestrator(tmp_path, fake_matrix_client, _make_llm_for_simple_complete)
    config = AgentConfig(name="solo", role=AgentRole.IMPLEMENTER, instructions="do it")
    task = Task(
        id="single-task-1",
        spec=_always_pass_spec(),
        description="trivial work",
        status=TaskStatus.PENDING,
    )
    result = await orch.single_task(config, task)
    assert result.task_id == "single-task-1"
    assert result.success is True


async def test_project_advances_to_done(tmp_path, fake_matrix_client) -> None:
    orch = _make_orchestrator(tmp_path, fake_matrix_client, _make_llm_for_simple_complete)
    agents = [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)]
    tasks = [
        Task(
            id=f"t{i}",
            spec=_always_pass_spec(),
            description=f"task {i}",
            agent_id="w",
            status=TaskStatus.PENDING,
        )
        for i in range(3)
    ]
    result = await orch.run_project("demo", agents, tasks)
    assert result.success is True
    assert result.project.phase == ProjectPhase.DONE
    # Phase history should include every phase traversed.
    phases = [c.to_phase for c in result.project.phase_history]
    assert ProjectPhase.ANALYSIS in phases
    assert ProjectPhase.REVIEW in phases
    assert ProjectPhase.DONE in phases


async def test_review_rejection_triggers_loopback(tmp_path, fake_matrix_client) -> None:
    orch = _make_orchestrator(tmp_path, fake_matrix_client, _make_llm_for_simple_complete)
    agents = [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)]
    tasks = [
        Task(id="t1", spec=_always_pass_spec(), description="x", agent_id="w")
    ]

    calls: list[int] = []

    async def review(project, results):
        calls.append(len(results))
        if len(calls) == 1:
            return ReviewDecision(
                approved=False,
                feedback="want more",
                return_to_phase=ProjectPhase.IMPLEMENTATION,
            )
        return ReviewDecision(approved=True)

    result = await orch.run_project("loopy", agents, tasks, review_fn=review, max_loopbacks=2)
    assert result.success is True
    # Two review cycles recorded.
    assert len(calls) == 2


async def test_loopback_limit_fails_project(tmp_path, fake_matrix_client) -> None:
    orch = _make_orchestrator(tmp_path, fake_matrix_client, _make_llm_for_simple_complete)
    agents = [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)]
    tasks = [Task(id="t1", spec=_always_pass_spec(), description="x", agent_id="w")]

    async def always_reject(project, results):
        return ReviewDecision(
            approved=False, feedback="bad", return_to_phase=ProjectPhase.IMPLEMENTATION
        )

    result = await orch.run_project(
        "unfixable", agents, tasks, review_fn=always_reject, max_loopbacks=1
    )
    assert result.success is False
    assert result.project.phase == ProjectPhase.FAILED


async def test_failing_postconditions_fail_project_via_auto_review(
    tmp_path, fake_matrix_client
) -> None:
    # Use fail spec so every task "completes" but postcondition fails.
    orch = _make_orchestrator(tmp_path, fake_matrix_client, _make_llm_for_simple_complete)
    agents = [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)]
    tasks = [Task(id="t1", spec=_fail_spec(), description="x", agent_id="w")]
    result = await orch.run_project("failing", agents, tasks, max_loopbacks=0)
    # Auto-review triggers a loopback attempt but max=0 → FAILED.
    assert result.success is False


async def test_in_phase_auto_retry_eventually_succeeds(
    tmp_path, fake_matrix_client
) -> None:
    """With max_task_retries>0, a task that fails then succeeds is auto-retried
    within the same phase and ultimately the project finishes without the
    cross-phase loopback machinery firing."""
    # Spec flips from fail → pass based on a counter, so the first attempt
    # fails its postcondition and the second attempt passes.
    from agora.core.types import ProjectPhase

    attempts = {"count": 0}

    def check(_ctx):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return False, "first attempt always fails"
        return True, ""

    spec = Specification(
        postconditions=(make_predicate("flip", "pass on retry", check),),
        description="flip",
    )
    orch = _make_orchestrator(tmp_path, fake_matrix_client, _make_llm_for_simple_complete)
    agents = [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)]
    tasks = [Task(id="t1", spec=spec, description="retryable", agent_id="w")]

    # max_loopbacks=0 ensures the cross-phase fallback is *off* — success must
    # come from the in-phase retry exclusively.
    result = await orch.run_project(
        "retryable", agents, tasks, max_loopbacks=0, max_task_retries=2
    )
    assert result.success is True
    assert result.project.phase == ProjectPhase.DONE
    assert attempts["count"] == 2, "check should have fired twice (fail → pass)"
    # Only the final outcome is kept per task.
    assert len(result.task_results) == 1
    assert result.task_results[0].success is True


async def test_in_phase_retries_exhaust_then_fail(
    tmp_path, fake_matrix_client
) -> None:
    """When retries are exhausted the task is marked FAILED and the project
    fails via the normal auto-review path."""
    orch = _make_orchestrator(tmp_path, fake_matrix_client, _make_llm_for_simple_complete)
    agents = [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)]
    tasks = [Task(id="t1", spec=_fail_spec(), description="unfixable", agent_id="w")]
    result = await orch.run_project(
        "doomed", agents, tasks, max_loopbacks=0, max_task_retries=2
    )
    assert result.success is False
    # Only the final attempt's outcome is retained.
    assert len(result.task_results) == 1
    assert result.task_results[0].success is False
