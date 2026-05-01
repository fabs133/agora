"""Tests for the narration-detection redirect.

Run 15 surfaced a model-behaviour failure mode: the agent describes its plan
(*"Let's read app.py to find an appropriate anchor"*) instead of calling the
tool. Auto-mark-complete fires with no artifacts, the task fails.

The orchestrator now detects that exact shape — task has an ``output_path``
but the artifacts never contain it — and queues a loud system-authored
redirect into ``control.task_comments[task.id]`` so the next retry's system
prompt opens with a STOP-PLANNING directive.
"""

from __future__ import annotations

from pathlib import Path

from agora.core.agent import AgentConfig
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole
from agora.fleet.agent_runtime import TaskResult
from agora.fleet.control import OrchestratorControl
from agora.fleet.orchestrator import Orchestrator, _output_path_was_produced
from agora.matrix.room_manager import RoomManager

# =============================================================================
# _output_path_was_produced — the core detector
# =============================================================================


def test_output_path_was_produced_exact_match() -> None:
    assert _output_path_was_produced("bot.py", ["bot.py"]) is True


def test_output_path_was_produced_substring_match() -> None:
    # Some tools report a fully-qualified path; we match by substring.
    assert _output_path_was_produced("bot.py", ["workspace/bot.py"]) is True


def test_output_path_was_produced_returns_false_on_empty_artifacts() -> None:
    assert _output_path_was_produced("bot.py", []) is False
    assert _output_path_was_produced("bot.py", None) is False  # type: ignore[arg-type]


def test_output_path_was_produced_returns_false_on_mismatch() -> None:
    assert _output_path_was_produced("bot.py", ["README.md"]) is False


def test_output_path_was_produced_skips_non_strings() -> None:
    # Defensive — sometimes artifacts lists include dicts or Nones.
    assert _output_path_was_produced("bot.py", [None, {"path": "bot.py"}, "other.py"]) is False  # type: ignore[list-item]


def test_output_path_was_produced_empty_path_is_false() -> None:
    assert _output_path_was_produced("", ["bot.py"]) is False


# =============================================================================
# _maybe_queue_narration_redirect — orchestrator integration
# =============================================================================


def _make_orchestrator(tmp_path: Path, fake_matrix_client) -> Orchestrator:
    return Orchestrator(
        matrix_client=fake_matrix_client,
        room_manager=RoomManager(fake_matrix_client, homeserver_name="agora.local"),
        llm_factory=lambda _m: None,  # never actually used in these unit tests
        work_dir=str(tmp_path / "work"),
        max_parallel_agents=1,
        enable_observer=False,
        skip_warmup=True,
    )


def _failed_task_and_outcome(output_path: str, artifacts: list[str]) -> tuple[Task, TaskResult]:
    spec = Specification(
        postconditions=(make_predicate("ok", "", lambda _c: (False, "mismatch")),),
        description="",
    )
    task = Task(
        id="build_thing",
        spec=spec,
        description="do the thing",
        agent_id="impl",
        output_path=output_path,
    )
    outcome = TaskResult(
        task_id="build_thing",
        success=False,
        output="Let's read app.py to find an appropriate anchor.",
        artifacts=artifacts,
    )
    return task, outcome


async def test_narration_redirect_queued_when_output_never_written(
    tmp_path: Path, fake_matrix_client
) -> None:
    orch = _make_orchestrator(tmp_path, fake_matrix_client)
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)
    task, outcome = _failed_task_and_outcome("app.py", artifacts=[])

    orch._maybe_queue_narration_redirect(task, outcome, control)

    queued = control.task_comments.get("build_thing", [])
    assert len(queued) == 1
    assert "[SYSTEM]" in queued[0]
    assert "narrated" in queued[0]
    assert "app.py" in queued[0]
    assert "tool_use" in queued[0]


async def test_narration_redirect_skipped_when_artifact_produced(
    tmp_path: Path, fake_matrix_client
) -> None:
    """If the task DID write its output_path (but still failed for another
    reason, e.g. content check), don't queue a redirect — the failure wasn't
    narration."""
    orch = _make_orchestrator(tmp_path, fake_matrix_client)
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)
    task, outcome = _failed_task_and_outcome("app.py", artifacts=["app.py"])

    orch._maybe_queue_narration_redirect(task, outcome, control)

    assert "build_thing" not in control.task_comments


async def test_narration_redirect_skipped_when_no_output_path(
    tmp_path: Path, fake_matrix_client
) -> None:
    """Tasks without a declared output_path (like integration_check) don't
    signal narration via empty artifacts — skip them."""
    orch = _make_orchestrator(tmp_path, fake_matrix_client)
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)
    task, outcome = _failed_task_and_outcome("", artifacts=[])

    orch._maybe_queue_narration_redirect(task, outcome, control)

    assert "build_thing" not in control.task_comments


async def test_narration_redirect_skipped_when_no_control(
    tmp_path: Path, fake_matrix_client
) -> None:
    orch = _make_orchestrator(tmp_path, fake_matrix_client)
    task, outcome = _failed_task_and_outcome("app.py", artifacts=[])
    # Should not raise with control=None.
    orch._maybe_queue_narration_redirect(task, outcome, None)


async def test_narration_redirect_deduplicates_on_repeated_failures(
    tmp_path: Path, fake_matrix_client
) -> None:
    """If the same task fails twice without writing output, the redirect is
    queued only once — we don't want to stack identical directives."""
    orch = _make_orchestrator(tmp_path, fake_matrix_client)
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)
    task, outcome = _failed_task_and_outcome("app.py", artifacts=[])

    orch._maybe_queue_narration_redirect(task, outcome, control)
    orch._maybe_queue_narration_redirect(task, outcome, control)

    queued = control.task_comments.get("build_thing", [])
    assert len(queued) == 1


async def test_narration_redirect_respects_preexisting_user_comments(
    tmp_path: Path, fake_matrix_client
) -> None:
    """User-authored comments via ``/agora comment`` already sit in
    task_comments — the system redirect is appended alongside, not replacing."""
    orch = _make_orchestrator(tmp_path, fake_matrix_client)
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)
    control.task_comments["build_thing"] = ["use random.randint instead"]
    task, outcome = _failed_task_and_outcome("app.py", artifacts=[])

    orch._maybe_queue_narration_redirect(task, outcome, control)

    queued = control.task_comments["build_thing"]
    assert len(queued) == 2
    assert queued[0] == "use random.randint instead"
    assert "[SYSTEM]" in queued[1]


async def test_system_prompt_includes_narration_redirect(
    tmp_path: Path, fake_matrix_client
) -> None:
    """End-to-end: after _maybe_queue_narration_redirect, the next call to
    _compose_system_prompt(task_id=X) puts the redirect into the prompt."""
    from agora.core.agent import AgentIdentity
    from agora.fleet.agent_runtime import AgentRuntime
    from agora.fleet.inner_tools import ToolContext
    from tests.conftest import FakeLLM

    orch = _make_orchestrator(tmp_path, fake_matrix_client)
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)

    task, outcome = _failed_task_and_outcome("app.py", artifacts=[])
    orch._maybe_queue_narration_redirect(task, outcome, control)

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=room,
        control=control,
    )
    identity = AgentIdentity(
        agent_id="w",
        room_id=agent_room,
        config=AgentConfig(name="w", role=AgentRole.IMPLEMENTER, instructions="base"),
    )
    runtime = AgentRuntime(llm=FakeLLM([]), matrix_client=fake_matrix_client, tool_context=ctx)
    prompt = runtime._compose_system_prompt(identity, task_id="build_thing")
    assert "Reviewer feedback on this specific task" in prompt
    assert "[SYSTEM]" in prompt
    assert "narrated" in prompt
    # One-shot consumption cleared the queue.
    assert control.task_comments.get("build_thing") is None
