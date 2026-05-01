"""Framework stage coverage for the v2.3 agent-builder fleet.

Covers the 5 new framework stage kinds introduced to let the 7B planner
narrow agent authoring into per-agent stages:

- ``plan_reset_agents`` — clears ``plan_draft.agents`` so task retries start
  from a clean roster.
- ``plan_snapshot_draft`` — writes a markdown snapshot of the current draft
  so subsequent LLM stages can reference real agent names + task ids.
- ``plan_validate_agent`` — compile-checks one agent by name/role.
- ``plan_validate_roster`` — roster-level checks (min count, builder role).
- ``plan_validate_agents_vs_tasks`` — cross-check every agent has ≥1 task.

No LLM, no Matrix traffic — each stage is a pure framework dispatch that
mutates or reads ``control.plan_draft``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus
from agora.fleet.agent_runtime import AgentRuntime
from agora.fleet.control import OrchestratorControl
from agora.fleet.inner_tools import ToolContext
from agora.fleet.stage_runner import Stage, StagedTask, StageRunner
from agora.plan.builder import PlanDraft


class _FakeMatrixClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def send_event(self, room_id: str, event_type: str, content: dict) -> str:
        self.sent.append((room_id, event_type, content))
        return f"$evt_{len(self.sent)}"


class _FakeLLM:
    """Agent-fleet framework stages never invoke the LLM."""

    async def complete(self, *args, **kwargs):
        raise AssertionError("framework stage should not invoke the LLM")


def _make_runtime(tmp_path: Path):
    client = _FakeMatrixClient()
    control = OrchestratorControl(
        project_room_id="!room:x", matrix_client=client  # type: ignore[arg-type]
    )
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=client,  # type: ignore[arg-type]
        agent_room_id="!agent:x",
        project_room_id="!room:x",
        control=control,
    )
    runtime = AgentRuntime(
        llm=_FakeLLM(),  # type: ignore[arg-type]
        matrix_client=client,  # type: ignore[arg-type]
        tool_context=ctx,
    )
    return runtime, control


@pytest.fixture
def _identity() -> AgentIdentity:
    return AgentIdentity(
        agent_id="architect",
        room_id="!agent:x",
        config=AgentConfig(name="architect", role=AgentRole.ARCHITECT),
    )


def _single_stage_task(stage: Stage, task_id: str = "t") -> StagedTask:
    """Wrap one framework stage in a trivial task for StageRunner.execute."""
    task = Task(
        id=task_id,
        spec=Specification(
            postconditions=(
                make_predicate("_trivial", "always true", lambda _c: (True, "")),
            ),
            description="framework stage test",
        ),
        description="framework stage test",
        agent_id="architect",
        status=TaskStatus.PENDING,
    )
    return StagedTask(task=task, stages=[stage])


# =====================================================  plan_reset_agents


async def test_reset_agents_clears_roster(tmp_path: Path, _identity):
    runtime, control = _make_runtime(tmp_path)
    draft = PlanDraft()
    draft.upsert_agent("a1", "architect", "writes designs with enough text to count")
    draft.upsert_agent("a2", "implementer", "writes code per the designs given")
    control.plan_draft = draft

    runner = StageRunner(runtime)
    staged = _single_stage_task(Stage(name="reset", kind="plan_reset_agents"))
    outcome = await runner.execute_staged_task(staged, _identity)

    assert outcome.success is True
    assert draft.agents == []


async def test_reset_agents_noop_without_draft(tmp_path: Path, _identity):
    """No plan_draft → reset logs and returns OK; doesn't raise."""
    runtime, _control = _make_runtime(tmp_path)
    runner = StageRunner(runtime)
    staged = _single_stage_task(Stage(name="reset", kind="plan_reset_agents"))
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True


# =====================================================  plan_snapshot_draft


async def test_snapshot_draft_writes_markdown(tmp_path: Path, _identity):
    runtime, control = _make_runtime(tmp_path)
    draft = PlanDraft()
    draft.upsert_agent("arch", "architect", "writes architectural outlines and contracts")
    draft.upsert_agent("impl", "implementer", "writes code per the architectural outlines")
    draft.add_task("setup", "Bootstrap the project", "arch", output_path="pyproject.toml")
    draft.attach_postcondition("setup", "mark_complete", {})
    control.plan_draft = draft

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="snap", kind="plan_snapshot_draft",
            output_path="plan/.draft_state.md",
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True

    written = tmp_path / "plan" / ".draft_state.md"
    assert written.is_file()
    body = written.read_text(encoding="utf-8")
    assert "## Agents (2)" in body
    assert "**arch** (role=architect)" in body
    assert "## Tasks (1)" in body
    assert "**setup** → arch" in body


async def test_snapshot_draft_fails_without_draft(tmp_path: Path, _identity):
    runtime, _control = _make_runtime(tmp_path)
    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(name="snap", kind="plan_snapshot_draft", output_path="x.md")
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    assert any(
        "plan_draft is empty" in r[2] for r in outcome.postcondition_results
    )


# =====================================================  plan_validate_agent


async def test_validate_agent_passes_valid(tmp_path: Path, _identity):
    runtime, control = _make_runtime(tmp_path)
    draft = PlanDraft()
    draft.upsert_agent(
        "impl", "implementer",
        "writes the CLI entry point and the core module based on the brief",
    )
    control.plan_draft = draft

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="v", kind="plan_validate_agent",
            validation_args={"expected_name": "impl", "expected_role": "implementer"},
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True, outcome.postcondition_results


async def test_validate_agent_fails_missing(tmp_path: Path, _identity):
    runtime, control = _make_runtime(tmp_path)
    control.plan_draft = PlanDraft()
    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="v", kind="plan_validate_agent",
            validation_args={"expected_name": "ghost", "expected_role": "architect"},
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    assert any("not in the draft roster" in r[2] for r in outcome.postcondition_results)


async def test_validate_agent_fails_short_instructions(tmp_path: Path, _identity):
    runtime, control = _make_runtime(tmp_path)
    draft = PlanDraft()
    draft.upsert_agent("a1", "architect", "tiny")
    control.plan_draft = draft
    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="v", kind="plan_validate_agent",
            validation_args={"expected_name": "a1", "expected_role": "architect"},
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    assert any("too short" in r[2] for r in outcome.postcondition_results)


async def test_validate_agent_fails_wrong_role(tmp_path: Path, _identity):
    runtime, control = _make_runtime(tmp_path)
    draft = PlanDraft()
    draft.upsert_agent("a1", "architect", "writes architectural outlines clearly")
    control.plan_draft = draft
    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="v", kind="plan_validate_agent",
            validation_args={"expected_name": "a1", "expected_role": "tester"},
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    assert any("expected role 'tester'" in r[2] for r in outcome.postcondition_results)


# =====================================================  plan_validate_roster


async def test_validate_roster_passes_two_agents(tmp_path: Path, _identity):
    runtime, control = _make_runtime(tmp_path)
    draft = PlanDraft()
    draft.upsert_agent("a1", "architect", "writes architectural outlines and contracts")
    draft.upsert_agent("i1", "implementer", "writes code per the architectural outlines")
    control.plan_draft = draft
    runner = StageRunner(runtime)
    staged = _single_stage_task(Stage(name="v", kind="plan_validate_roster"))
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True


async def test_validate_roster_fails_single_agent(tmp_path: Path, _identity):
    runtime, control = _make_runtime(tmp_path)
    draft = PlanDraft()
    draft.upsert_agent("a1", "architect", "writes architectural outlines and contracts")
    control.plan_draft = draft
    runner = StageRunner(runtime)
    staged = _single_stage_task(Stage(name="v", kind="plan_validate_roster"))
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    assert any("need ≥ 2" in r[2] for r in outcome.postcondition_results)


# =====================================================  plan_validate_agents_vs_tasks


async def test_validate_agents_vs_tasks_passes(tmp_path: Path, _identity):
    runtime, control = _make_runtime(tmp_path)
    draft = PlanDraft()
    draft.upsert_agent("arch", "architect", "writes the architectural outline and contracts")
    draft.upsert_agent("impl", "implementer", "writes the CLI code and core module")
    draft.add_task("setup", "d", "arch")
    draft.attach_postcondition("setup", "mark_complete", {})
    draft.add_task("build", "d", "impl", depends_on=["setup"])
    draft.attach_postcondition("build", "mark_complete", {})
    control.plan_draft = draft

    runner = StageRunner(runtime)
    staged = _single_stage_task(Stage(name="v", kind="plan_validate_agents_vs_tasks"))
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True


async def test_link_tasks_to_agents_rebalances_via_stage(tmp_path: Path, _identity):
    """``kind: plan_link_tasks_to_agents`` is a no-op if assignments are
    already clean, and rebalances tasks from over-loaded agents to orphans
    otherwise. Zero LLM."""
    runtime, control = _make_runtime(tmp_path)
    draft = PlanDraft()
    draft.upsert_agent("arch", "architect", "writes the architectural outlines and postconds")
    draft.upsert_agent("impl", "implementer", "implements the CLI entry and core modules")
    draft.upsert_agent("test", "tester", "writes pytest tests for the cli")
    # All three tasks land on the implementer — arch + test become orphans.
    draft.add_task("setup_project", "Scaffold the package", "impl", output_path="pyproject.toml")
    draft.attach_postcondition("setup_project", "mark_complete", {})
    draft.add_task("build_cli", "Build the CLI", "impl", output_path="src/cli.py")
    draft.attach_postcondition("build_cli", "mark_complete", {})
    draft.add_task("write_tests", "Write pytest cases", "impl", output_path="tests/test_cli.py")
    draft.attach_postcondition("write_tests", "mark_complete", {})
    control.plan_draft = draft

    runner = StageRunner(runtime)
    staged = _single_stage_task(Stage(name="link", kind="plan_link_tasks_to_agents"))
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True

    # After the linker, every agent has ≥1 task.
    owners = {t["assigned_to"] for t in draft.tasks.values()}
    assert owners == {"arch", "impl", "test"}, owners
    assert draft.validate_agents_vs_tasks() == []


async def test_validate_agents_vs_tasks_fails_orphan(tmp_path: Path, _identity):
    runtime, control = _make_runtime(tmp_path)
    draft = PlanDraft()
    draft.upsert_agent("arch", "architect", "writes the architectural outline and contracts")
    draft.upsert_agent("tester", "tester", "writes pytest tests covering deliverables")
    draft.add_task("t1", "d", "arch")
    draft.attach_postcondition("t1", "mark_complete", {})
    control.plan_draft = draft

    runner = StageRunner(runtime)
    staged = _single_stage_task(Stage(name="v", kind="plan_validate_agents_vs_tasks"))
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    assert any("no tasks assigned" in r[2] for r in outcome.postcondition_results)
    assert any("tester" in r[2] for r in outcome.postcondition_results)
