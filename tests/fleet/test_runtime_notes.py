"""Verify agent_runtime picks up observer notes + redirects through ToolContext."""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.contract import Specification
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus
from agora.fleet.agent_runtime import AgentRuntime
from agora.fleet.control import OrchestratorControl
from agora.fleet.inner_tools import ToolContext
from agora.fleet.llm_adapter import LLMResponse
from tests.conftest import FakeLLM


@pytest.fixture
async def fixture(fake_matrix_client, tmp_path: Path):
    agent_room = await fake_matrix_client.create_room("agent")
    project_room = await fake_matrix_client.create_room("proj")
    identity = AgentIdentity(
        agent_id="@impl:agora.local",
        room_id=agent_room,
        config=AgentConfig(name="impl", role=AgentRole.IMPLEMENTER, instructions="base"),
    )
    return fake_matrix_client, tmp_path, identity, agent_room, project_room


async def test_runtime_injects_observer_note(fixture) -> None:
    client, tmp_path, identity, _agent_room, project_room = fixture
    control = OrchestratorControl(project_room_id=project_room, matrix_client=client)
    control.notes.append("prefer functional style")

    llm = FakeLLM(
        [
            LLMResponse(content="ok"),  # no tools → terminate
            LLMResponse(content="[]"),  # reflection
        ]
    )
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=client,
        agent_room_id=identity.room_id,
        project_room_id=project_room,
        control=control,
    )
    runtime = AgentRuntime(llm=llm, matrix_client=client, tool_context=ctx)
    task = Task(
        id="t1", spec=Specification(), description="do it", status=TaskStatus.PENDING
    )
    await runtime.execute_task(task, identity)

    # Inspect the LLM's *actual* system prompt (captured by FakeLLM).
    assert llm.calls
    system_prompt = llm.calls[0]["system"]
    assert "prefer functional style" in system_prompt
    assert "Observer notes" in system_prompt


async def test_runtime_injects_and_clears_redirect(fixture) -> None:
    client, tmp_path, identity, _agent_room, project_room = fixture
    control = OrchestratorControl(project_room_id=project_room, matrix_client=client)
    control.agent_redirects["impl"] = "focus on error handling"

    llm = FakeLLM([LLMResponse(content="ok"), LLMResponse(content="[]")])
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=client,
        agent_room_id=identity.room_id,
        project_room_id=project_room,
        control=control,
    )
    runtime = AgentRuntime(llm=llm, matrix_client=client, tool_context=ctx)
    task = Task(id="t1", spec=Specification(), description="do", status=TaskStatus.PENDING)
    await runtime.execute_task(task, identity)

    system_prompt = llm.calls[0]["system"]
    assert "Observer redirect" in system_prompt
    assert "error handling" in system_prompt
    # One-shot: redirect gone after consumption.
    assert "impl" not in control.agent_redirects


async def test_runtime_without_control_is_unchanged(fixture) -> None:
    client, tmp_path, identity, _agent_room, project_room = fixture
    llm = FakeLLM([LLMResponse(content="ok"), LLMResponse(content="[]")])
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=client,
        agent_room_id=identity.room_id,
        project_room_id=project_room,
    )
    runtime = AgentRuntime(llm=llm, matrix_client=client, tool_context=ctx)
    task = Task(id="t1", spec=Specification(), description="do", status=TaskStatus.PENDING)
    await runtime.execute_task(task, identity)

    # Runtime falls back to identity.effective_instructions when there's no control.
    system_prompt = llm.calls[0]["system"]
    assert system_prompt == identity.effective_instructions
