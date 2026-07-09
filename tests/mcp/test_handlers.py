"""Tests for the MCP handlers layer.

The orchestrator is wired with ``FakeMatrixClient`` (from root conftest) and a
``FakeLLM`` that always mark_completes. We exercise the handler methods
directly, not through the FastMCP transport — transport is a thin facade.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.core.errors import AgoraError
from agora.core.types import ProjectPhase, TaskStatus
from agora.fleet.llm_adapter import LLMResponse
from agora.fleet.orchestrator import Orchestrator
from agora.matrix.room_manager import RoomManager
from agora.mcp.handlers import AgoraHandlers
from tests.conftest import FakeLLM, tool_call


def _llm_plan_factory():
    # Mark complete → stop → reflection empty list. Replayed many times.
    return FakeLLM(
        [
            LLMResponse(
                content="",
                tool_calls=(tool_call("mark_complete", {"summary": "done"}),),
            ),
            LLMResponse(content="complete"),
            LLMResponse(content="[]"),
        ]
        * 20
    )


@pytest.fixture
def handlers(tmp_path: Path, fake_matrix_client) -> AgoraHandlers:
    room_manager = RoomManager(fake_matrix_client, homeserver_name="agora.local")
    orchestrator = Orchestrator(
        matrix_client=fake_matrix_client,
        room_manager=room_manager,
        llm_factory=lambda _m: _llm_plan_factory(),
        work_dir=str(tmp_path / "work"),
        skip_warmup=True,  # fake LLM — no real Ollama in unit tests
    )
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    return AgoraHandlers(orchestrator, flows_dir=tmp_path / "flows")


# -------------------------------------------------------------- spawn_agent

async def test_spawn_agent_creates_identity_room(handlers: AgoraHandlers) -> None:
    result = await handlers.spawn_agent(
        {"name": "alice", "role": "architect", "instructions": "design stuff"}
    )
    assert result["name"] == "alice"
    assert result["agent_id"] == "@alice:agora.local"
    assert result["room_id"].startswith("!")
    assert result["agent_id"] in handlers.agents


async def test_spawn_agent_rejects_invalid_role(handlers: AgoraHandlers) -> None:
    with pytest.raises(AgoraError, match="invalid agent role"):
        await handlers.spawn_agent({"name": "x", "role": "wizard"})


async def test_spawn_agent_requires_name(handlers: AgoraHandlers) -> None:
    with pytest.raises(AgoraError, match="required field 'name'"):
        await handlers.spawn_agent({"role": "architect"})


# -------------------------------------------------------------- assign_task

async def test_assign_task_runs_synchronously(handlers: AgoraHandlers) -> None:
    await handlers.spawn_agent({"name": "worker", "role": "implementer"})
    result = await handlers.assign_task(
        {"agent_name": "worker", "description": "do a thing"}
    )
    assert result["success"] is True
    assert result["task_id"] in handlers.tasks
    assert handlers.tasks[result["task_id"]].status == TaskStatus.DONE


async def test_assign_task_unknown_agent(handlers: AgoraHandlers) -> None:
    with pytest.raises(AgoraError, match="no agent named"):
        await handlers.assign_task({"agent_name": "ghost", "description": "nope"})


# -------------------------------------------------------------- run_project

async def test_run_project_launches_and_completes(handlers: AgoraHandlers) -> None:
    result = await handlers.run_project(
        {
            "name": "demo",
            "agents": [{"name": "w", "role": "implementer"}],
            "tasks": [
                {"description": "task 1", "agent_id": "w"},
                {"description": "task 2", "agent_id": "w"},
            ],
        }
    )
    project_id = result["project_id"]
    assert result["phase"] == ProjectPhase.INIT.value

    # Let the background task complete.
    entry = handlers.projects[project_id]
    await entry.task  # type: ignore[arg-type]
    assert entry.phase == ProjectPhase.DONE


async def test_run_project_requires_agents_and_tasks(handlers: AgoraHandlers) -> None:
    with pytest.raises(AgoraError, match="at least one agent"):
        await handlers.run_project({"name": "x", "agents": [], "tasks": [{"description": "x"}]})
    with pytest.raises(AgoraError, match="at least one task"):
        await handlers.run_project(
            {"name": "x", "agents": [{"name": "a", "role": "implementer"}], "tasks": []}
        )


# -------------------------------------------------------------- create_flow / run_flow

async def test_create_flow_persists_yaml(handlers: AgoraHandlers, tmp_path: Path) -> None:
    result = await handlers.create_flow(
        {
            "name": "design-then-build",
            "description": "two-step flow",
            "agents": [
                {"name": "architect", "role": "architect"},
                {"name": "impl", "role": "implementer"},
            ],
            "task_graph": [
                {"id": "plan", "assigned_to": "architect", "description": "plan it"},
                {
                    "id": "build",
                    "assigned_to": "impl",
                    "description": "build it",
                    "depends_on": ["plan"],
                },
            ],
        }
    )
    assert Path(result["path"]).is_file()
    assert "design-then-build" in handlers.flows


async def test_run_flow_launches_project(handlers: AgoraHandlers) -> None:
    await handlers.create_flow(
        {
            "name": "solo",
            "description": "",
            "agents": [{"name": "w", "role": "implementer"}],
            "task_graph": [
                {"id": "t1", "assigned_to": "w", "description": "do it"}
            ],
        }
    )
    result = await handlers.run_flow({"flow_name": "solo"})
    project_id = result["project_id"]
    await handlers.projects[project_id].task  # type: ignore[arg-type]
    assert handlers.projects[project_id].phase == ProjectPhase.DONE


async def test_run_flow_unknown_raises(handlers: AgoraHandlers) -> None:
    with pytest.raises(AgoraError, match="unknown flow"):
        await handlers.run_flow({"flow_name": "missing"})


# -------------------------------------------------------------- agent_status

async def test_agent_status_returns_everything_when_no_args(handlers: AgoraHandlers) -> None:
    await handlers.spawn_agent({"name": "alice", "role": "architect"})
    status = await handlers.agent_status({})
    assert len(status["agents"]) == 1
    assert status["agents"][0]["name"] == "alice"


async def test_agent_status_by_agent_name(handlers: AgoraHandlers) -> None:
    await handlers.spawn_agent({"name": "alice", "role": "architect"})
    await handlers.spawn_agent({"name": "bob", "role": "implementer"})
    await handlers.assign_task({"agent_name": "bob", "description": "t1"})
    status = await handlers.agent_status({"agent_name": "bob"})
    assert status["agent"]["name"] == "bob"
    assert len(status["tasks"]) == 1


async def test_agent_status_by_project_id(handlers: AgoraHandlers) -> None:
    launched = await handlers.run_project(
        {
            "name": "demo",
            "agents": [{"name": "w", "role": "implementer"}],
            "tasks": [{"description": "t1", "agent_id": "w"}],
        }
    )
    status = await handlers.agent_status({"project_id": launched["project_id"]})
    assert status["id"] == launched["project_id"]
    assert status["name"] == "demo"


# ------------------------------------------------------------------ kanban

async def test_get_kanban_groups_by_status(handlers: AgoraHandlers) -> None:
    await handlers.spawn_agent({"name": "w", "role": "implementer"})
    await handlers.assign_task({"agent_name": "w", "description": "done task"})

    board = await handlers.get_kanban({})
    assert TaskStatus.DONE.value in board["columns"]
    assert len(board["columns"][TaskStatus.DONE.value]) == 1


async def test_get_kanban_filtered_by_project(handlers: AgoraHandlers) -> None:
    launched = await handlers.run_project(
        {
            "name": "demo",
            "agents": [{"name": "w", "role": "implementer"}],
            "tasks": [{"description": "t1", "agent_id": "w"}],
        }
    )
    await handlers.projects[launched["project_id"]].task  # type: ignore[arg-type]
    board = await handlers.get_kanban({"project_id": launched["project_id"]})
    all_tasks = sum(len(v) for v in board["columns"].values())
    assert all_tasks == 1


# ------------------------------------------------------------------ export_report

async def test_export_report_writes_html(handlers: AgoraHandlers, tmp_path: Path) -> None:
    launched = await handlers.run_project(
        {
            "name": "reportable",
            "agents": [{"name": "w", "role": "implementer"}],
            "tasks": [{"description": "t1", "agent_id": "w"}],
        }
    )
    await handlers.projects[launched["project_id"]].task  # type: ignore[arg-type]

    out = tmp_path / "report.html"
    result = await handlers.export_report(
        {"project_id": launched["project_id"], "path": str(out)}
    )
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    # Standalone HTML document with project metadata.
    assert "<!DOCTYPE html>" in text
    assert "reportable" in text
    assert "Kanban" in text
    assert result["path"] == str(out)


async def test_export_report_unknown_project(handlers: AgoraHandlers) -> None:
    with pytest.raises(AgoraError, match="unknown project_id"):
        await handlers.export_report({"project_id": "does-not-exist"})
