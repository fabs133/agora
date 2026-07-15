"""Integration tests for v2.5 scope-bounded error routing in the orchestrator.

Covers the ``_maybe_route_upstream_error`` helper and the ``_run_phase``
integration that flips an owning task back to PENDING, soft-passes the
downstream tester's pytest_passes postcondition, and re-verifies once
the owner retries successfully.
"""

from __future__ import annotations

from pathlib import Path

from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import TaskStatus
from agora.fleet.llm_adapter import LLMResponse
from agora.fleet.orchestrator import Orchestrator
from agora.matrix.room_manager import RoomManager
from tests.conftest import TEST_OLLAMA_URL, FakeLLM, tool_call


def _simple_llm() -> FakeLLM:
    """Replays enough 'tool_call mark_complete → stop → empty reflection'
    triples for a handful of task dispatches."""
    return FakeLLM(
        [
            LLMResponse(
                content="",
                tool_calls=(tool_call("mark_complete", {"summary": "done"}),),
            ),
            LLMResponse(content="done"),
            LLMResponse(content="[]"),
        ]
        * 10
    )


def _orchestrator(tmp_path: Path, fake_matrix_client, *, routed_budget: int = 2):
    mgr = RoomManager(fake_matrix_client, homeserver_name="agora.local")

    def factory(_model: str):
        return _simple_llm()

    return Orchestrator(
        matrix_client=fake_matrix_client,
        room_manager=mgr,
        llm_factory=factory,
        work_dir=str(tmp_path),
        routed_retry_budget=routed_budget,
        skip_warmup=True,
        ollama_base_url=TEST_OLLAMA_URL,
    )


def _owner_spec() -> Specification:
    """Owner always passes its own postcondition."""
    return Specification(
        postconditions=(
            make_predicate("owner_ok", "always true", lambda _c: (True, "")),
        ),
        description="owner",
    )


def _pytest_failure_spec(failing_module: str = "src.ghost") -> Specification:
    """A postcondition that mimics pytest_passes failing with a specific
    ModuleNotFoundError. The predicate name starts with 'pytest_' so the
    router classifies it correctly."""

    def check(_ctx):
        return (
            False,
            (
                f"pytest failed for tests/: exit 1\n"
                f"--- stdout ---\n"
                f"E   ModuleNotFoundError: No module named {failing_module!r}\n"
            ),
        )

    return Specification(
        postconditions=(make_predicate("pytest_passes_fake", "", check),),
        description="test",
    )


# =============================================================== direct unit test


def test_maybe_route_upstream_error_matches_owner(tmp_path, fake_matrix_client):
    """Given an outcome whose failure reason names ``src.ghost``, and a task
    list where one task owns ``src/ghost.py``, the router returns that owner."""
    from agora.fleet.agent_runtime import TaskResult

    orch = _orchestrator(tmp_path, fake_matrix_client)
    owner = Task(
        id="owner",
        spec=_owner_spec(),
        description="owns",
        agent_id="w",
        output_path="src/ghost.py",
        status=TaskStatus.DONE,
    )
    tester = Task(
        id="tester",
        spec=_pytest_failure_spec(),
        description="tests",
        agent_id="w",
        output_path="tests/test_x.py",
        status=TaskStatus.PENDING,
    )
    outcome = TaskResult(
        task_id="tester",
        success=False,
        output="",
        postcondition_results=[
            (
                "pytest_passes_fake",
                False,
                "pytest failed for tests/: exit 1\nE   ModuleNotFoundError: No module named 'src.ghost'",
            ),
        ],
    )
    routed_retries: dict[str, int] = {}
    descriptor = orch._maybe_route_upstream_error(
        tester, outcome, [owner, tester], routed_retries
    )
    assert descriptor is not None
    assert descriptor["owning_task_id"] == "owner"
    assert "src/ghost.py" in descriptor["owning_path"]
    assert "No module named" in descriptor["reason_excerpt"]
    assert "[SYSTEM]" in descriptor["feedback"]


def test_maybe_route_upstream_error_caps_at_budget(tmp_path, fake_matrix_client):
    """Once the owner has been routed ``routed_retry_budget`` times in this
    phase, subsequent route attempts return None so the test's normal retry
    path can take over (or the failure surfaces cleanly)."""
    from agora.fleet.agent_runtime import TaskResult

    orch = _orchestrator(tmp_path, fake_matrix_client, routed_budget=1)
    owner = Task(
        id="owner",
        spec=_owner_spec(),
        description="owns",
        agent_id="w",
        output_path="src/ghost.py",
        status=TaskStatus.DONE,
    )
    tester = Task(
        id="tester",
        spec=_pytest_failure_spec(),
        description="tests",
        agent_id="w",
        output_path="tests/test_x.py",
        status=TaskStatus.PENDING,
    )
    outcome = TaskResult(
        task_id="tester",
        success=False,
        output="",
        postcondition_results=[
            (
                "pytest_passes",
                False,
                "E   ModuleNotFoundError: No module named 'src.ghost'",
            ),
        ],
    )
    routed_retries = {"owner": 1}  # already at budget
    descriptor = orch._maybe_route_upstream_error(
        tester, outcome, [owner, tester], routed_retries
    )
    assert descriptor is None


def test_maybe_route_upstream_error_no_pytest_failure(tmp_path, fake_matrix_client):
    """A task whose failure isn't a pytest_passes doesn't get routed."""
    from agora.fleet.agent_runtime import TaskResult

    orch = _orchestrator(tmp_path, fake_matrix_client)
    owner = Task(
        id="owner",
        spec=_owner_spec(),
        description="owns",
        agent_id="w",
        output_path="src/ghost.py",
        status=TaskStatus.DONE,
    )
    tester = Task(
        id="tester",
        spec=_pytest_failure_spec(),
        description="tests",
        agent_id="w",
        status=TaskStatus.PENDING,
    )
    outcome = TaskResult(
        task_id="tester",
        success=False,
        output="",
        postcondition_results=[
            ("file_exists", False, "expected artifact containing 'x.py'"),
        ],
    )
    descriptor = orch._maybe_route_upstream_error(
        tester, outcome, [owner, tester], {}
    )
    assert descriptor is None


def test_maybe_route_upstream_error_never_routes_to_self(tmp_path, fake_matrix_client):
    """A test task claiming src/ghost.py as its own output shouldn't route
    back to itself."""
    from agora.fleet.agent_runtime import TaskResult

    orch = _orchestrator(tmp_path, fake_matrix_client)
    # The only task that owns src/ghost.py is the test task itself.
    tester = Task(
        id="tester",
        spec=_pytest_failure_spec(),
        description="tests",
        agent_id="w",
        output_path="src/ghost.py",  # same file the error points at
        status=TaskStatus.PENDING,
    )
    outcome = TaskResult(
        task_id="tester",
        success=False,
        output="",
        postcondition_results=[
            (
                "pytest_passes",
                False,
                "E   ModuleNotFoundError: No module named 'src.ghost'",
            ),
        ],
    )
    descriptor = orch._maybe_route_upstream_error(
        tester, outcome, [tester], {}
    )
    assert descriptor is None


def test_routed_retry_budget_disabled_when_zero(tmp_path, fake_matrix_client):
    """routed_retry_budget=0 disables routing entirely."""
    from agora.fleet.agent_runtime import TaskResult

    orch = _orchestrator(tmp_path, fake_matrix_client, routed_budget=0)
    owner = Task(
        id="owner",
        spec=_owner_spec(),
        description="owns",
        agent_id="w",
        output_path="src/ghost.py",
        status=TaskStatus.DONE,
    )
    tester = Task(
        id="tester",
        spec=_pytest_failure_spec(),
        description="tests",
        agent_id="w",
        status=TaskStatus.PENDING,
    )
    outcome = TaskResult(
        task_id="tester",
        success=False,
        output="",
        postcondition_results=[
            (
                "pytest_passes",
                False,
                "E   ModuleNotFoundError: No module named 'src.ghost'",
            ),
        ],
    )
    descriptor = orch._maybe_route_upstream_error(
        tester, outcome, [owner, tester], {}
    )
    assert descriptor is None
