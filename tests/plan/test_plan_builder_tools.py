"""End-to-end coverage for the six plan-authoring inner tools.

Each test exercises the tool through ``get_tool_executor`` — same path the
LLM's agent_runtime uses — so argument parsing, validation, and error
formatting match real runtime behaviour.

No live Matrix; a trivial fake client satisfies ``ToolContext`` requirements.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agora.core.types import AgentRole
from agora.fleet.inner_tools import ToolContext, get_tool_executor
from agora.plan.builder import PlanDraft


class _FakeMatrixClient:
    async def send_event(self, room_id: str, event_type: str, content: dict) -> str:
        return "$evt"


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        work_dir=str(tmp_path),
        matrix_client=_FakeMatrixClient(),  # type: ignore[arg-type]
        agent_room_id="!agent:x",
        project_room_id="!room:x",
    )


def _executor(ctx: ToolContext):
    return get_tool_executor(AgentRole.ARCHITECT, ctx)


# ---------------------------------------------------------------------- setup


async def test_plan_upsert_agent_ok(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    result = await exe["plan_upsert_agent"](
        {
            "name": "arch",
            "role": "architect",
            "instructions": "writes the architecture documents and postconditions",
        }
    )
    assert result.startswith("OK:"), result
    assert "added" in result
    assert ctx.plan_draft.agents[0]["name"] == "arch"


async def test_plan_upsert_agent_idempotent(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_upsert_agent"]({
        "name": "arch", "role": "architect",
        "instructions": "initial architecture role",
    })
    result = await exe["plan_upsert_agent"]({
        "name": "arch", "role": "architect",
        "instructions": "refined architecture role",
    })
    assert "updated" in result, result
    assert len(ctx.plan_draft.agents) == 1
    assert "refined" in ctx.plan_draft.agents[0]["instructions"]


async def test_plan_upsert_agent_rejects_bad_role(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    result = await exe["plan_upsert_agent"]({
        "name": "arch", "role": "wizard", "instructions": "cast",
    })
    assert result.startswith("ERROR:"), result
    assert "unknown role" in result


async def test_plan_set_agents_ok(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    result = await exe["plan_set_agents"](
        {"agents": [{"name": "a", "role": "architect", "instructions": ""}]}
    )
    assert result.startswith("OK:"), result
    assert isinstance(ctx.plan_draft, PlanDraft)
    assert ctx.plan_draft.agents[0]["name"] == "a"


async def test_plan_set_agents_error_surfaces(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    result = await exe["plan_set_agents"]({"agents": []})
    assert result.startswith("ERROR:"), result


async def test_plan_add_task_ok(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_set_agents"]({"agents": [{"name": "a", "role": "architect"}]})
    result = await exe["plan_add_task"](
        {"task_id": "t1", "description": "do", "assigned_to": "a"}
    )
    assert result.startswith("OK:"), result
    assert "t1" in ctx.plan_draft.tasks


async def test_plan_add_task_missing_required(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    # Missing assigned_to
    result = await exe["plan_add_task"]({"task_id": "t1", "description": "d"})
    assert result.startswith("ERROR:"), result


async def test_plan_add_task_unknown_agent(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_set_agents"]({"agents": [{"name": "a", "role": "architect"}]})
    result = await exe["plan_add_task"](
        {"task_id": "t1", "description": "d", "assigned_to": "missing"}
    )
    assert "ERROR:" in result
    assert "not a registered agent" in result


# ---------------------------------------------------------------- postconditions


async def test_plan_attach_postcondition_ok(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_set_agents"]({"agents": [{"name": "a", "role": "architect"}]})
    await exe["plan_add_task"]({"task_id": "t1", "description": "d", "assigned_to": "a"})
    result = await exe["plan_attach_postcondition"](
        {"task_id": "t1", "name": "mark_complete", "args": {}}
    )
    assert result.startswith("OK:"), result
    assert len(ctx.plan_draft.tasks["t1"]["postconditions"]) == 1


async def test_plan_attach_postcondition_unknown_predicate(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_set_agents"]({"agents": [{"name": "a", "role": "architect"}]})
    await exe["plan_add_task"]({"task_id": "t1", "description": "d", "assigned_to": "a"})
    result = await exe["plan_attach_postcondition"](
        {"task_id": "t1", "name": "bogus_predicate", "args": {}}
    )
    assert result.startswith("ERROR:"), result


async def test_plan_attach_postcondition_bad_args(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_set_agents"]({"agents": [{"name": "a", "role": "architect"}]})
    await exe["plan_add_task"]({"task_id": "t1", "description": "d", "assigned_to": "a"})
    # file_contains needs substring; missing → factory raises TypeError.
    result = await exe["plan_attach_postcondition"](
        {"task_id": "t1", "name": "file_contains", "args": {"rel": "x.md"}}
    )
    assert result.startswith("ERROR:"), result
    assert "invalid" in result.lower()


# ---------------------------------------------------------------- stages


async def test_plan_add_llm_stage_ok(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_set_agents"]({"agents": [{"name": "a", "role": "architect"}]})
    await exe["plan_add_task"]({"task_id": "t1", "description": "d", "assigned_to": "a"})
    result = await exe["plan_add_llm_stage"](
        {
            "task_id": "t1",
            "name": "write",
            "instruction": "do it",
            "max_iterations": 3,
        }
    )
    assert result.startswith("OK:"), result
    assert ctx.plan_draft.tasks["t1"]["stages"][0]["kind"] == "llm"


async def test_plan_add_decision_stage_ok(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_set_agents"]({"agents": [{"name": "a", "role": "architect"}]})
    await exe["plan_add_task"]({"task_id": "t1", "description": "d", "assigned_to": "a"})
    result = await exe["plan_add_decision_stage"](
        {
            "task_id": "t1",
            "name": "ask",
            "decision_id": "d1",
            "question": "Which?",
            "options": ["a", "b"],
            "output_path": "plan/ans.txt",
        }
    )
    assert result.startswith("OK:"), result
    stage = ctx.plan_draft.tasks["t1"]["stages"][0]
    assert stage["kind"] == "decision"
    assert stage["options"] == ["a", "b"]


# ---------------------------------------------------------------- finalize


async def _build_valid_draft(ctx: ToolContext) -> None:
    """Helper: populate a 2-task valid draft via tool calls."""
    exe = _executor(ctx)
    await exe["plan_set_agents"](
        {
            "agents": [
                {"name": "a", "role": "architect", "instructions": ""},
                {"name": "b", "role": "implementer", "instructions": ""},
            ]
        }
    )
    await exe["plan_add_task"](
        {
            "task_id": "fetch",
            "description": "fetch",
            "assigned_to": "a",
            "output_path": "kb/x.md",
        }
    )
    await exe["plan_attach_postcondition"](
        {"task_id": "fetch", "name": "file_exists", "args": {"rel": "kb/x.md"}}
    )
    await exe["plan_attach_postcondition"](
        {"task_id": "fetch", "name": "mark_complete", "args": {}}
    )
    await exe["plan_add_task"](
        {
            "task_id": "build",
            "description": "build",
            "assigned_to": "b",
            "depends_on": ["fetch"],
            "output_path": "out.py",
        }
    )
    await exe["plan_attach_postcondition"](
        {"task_id": "build", "name": "file_exists", "args": {"rel": "out.py"}}
    )
    await exe["plan_attach_postcondition"](
        {"task_id": "build", "name": "mark_complete", "args": {}}
    )


async def test_plan_finalize_round_trips_cleanly(tmp_path: Path):
    ctx = _ctx(tmp_path)
    await _build_valid_draft(ctx)
    exe = _executor(ctx)
    result = await exe["plan_finalize"](
        {"output_path": "plan/out.plan.yaml", "name": "rt-test"}
    )
    assert result.startswith("OK:"), result
    emitted = tmp_path / "plan/out.plan.yaml"
    assert emitted.is_file()
    # Verify the YAML actually loads + instantiates via the public loader.
    from agora.plan.loader import instantiate_plan, load_plan

    plan = load_plan(emitted)
    agents, tasks, _ = instantiate_plan(plan, project_name="rt-check")
    assert len(agents) == 2
    assert [t.id for t in tasks] == ["fetch", "build"]


async def test_plan_finalize_rejects_empty_draft(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    result = await exe["plan_finalize"]({"output_path": "plan/out.plan.yaml"})
    assert result.startswith("ERROR:"), result
    assert "no agents" in result


async def test_plan_finalize_records_artifact_and_completion(tmp_path: Path):
    ctx = _ctx(tmp_path)
    await _build_valid_draft(ctx)
    exe = _executor(ctx)
    await exe["plan_finalize"]({"output_path": "plan/out.plan.yaml", "name": "arts"})
    assert "plan/out.plan.yaml" in ctx.written_files
    assert any(c["artifacts"] == ["plan/out.plan.yaml"] for c in ctx.completions)


async def test_plan_finalize_rejects_tasks_without_postconditions(tmp_path: Path):
    """Strict validator: finalize refuses drafts where any task lacks a
    postcondition. The compound ``plan_add_task_spec`` tool makes this
    easy to avoid — each per-task stage attaches postconditions in the
    same call that adds the task."""
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_set_agents"]({"agents": [{"name": "a", "role": "architect"}]})
    await exe["plan_add_task"](
        {"task_id": "t1", "description": "d", "assigned_to": "a"}
    )
    result = await exe["plan_finalize"]({"output_path": "plan/out.plan.yaml"})
    assert result.startswith("ERROR:"), result
    assert "without postconditions" in result


# ------------------------------------------------------------ compound tool


async def test_plan_add_task_spec_atomic_ok(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_set_agents"]({"agents": [{"name": "a", "role": "architect"}]})
    result = await exe["plan_add_task_spec"](
        {
            "task_id": "build",
            "description": "build it",
            "assigned_to": "a",
            "output_path": "out.py",
            "postconditions": [
                {"name": "file_exists", "args": {"rel": "out.py"}},
                {"name": "mark_complete", "args": {}},
            ],
        }
    )
    assert result.startswith("OK:"), result
    assert "build" in ctx.plan_draft.tasks
    assert len(ctx.plan_draft.tasks["build"]["postconditions"]) == 2


async def test_plan_add_task_spec_rolls_back_bad_postcondition(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_set_agents"]({"agents": [{"name": "a", "role": "architect"}]})
    result = await exe["plan_add_task_spec"](
        {
            "task_id": "build",
            "description": "d",
            "assigned_to": "a",
            "postconditions": [
                {"name": "file_contains", "args": {"rel": "x.md"}},  # missing substring
            ],
        }
    )
    assert result.startswith("ERROR:"), result
    # Rollback — task not persisted.
    assert "build" not in ctx.plan_draft.tasks


async def test_plan_add_task_spec_requires_postconditions(tmp_path: Path):
    ctx = _ctx(tmp_path)
    exe = _executor(ctx)
    await exe["plan_set_agents"]({"agents": [{"name": "a", "role": "architect"}]})
    result = await exe["plan_add_task_spec"](
        {
            "task_id": "build",
            "description": "d",
            "assigned_to": "a",
            "postconditions": [],
        }
    )
    assert result.startswith("ERROR:"), result
    assert "non-empty list" in result
