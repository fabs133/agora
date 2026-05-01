"""Tests for the structured output-path banner.

Weak LLMs repeatedly wrote the right file to the wrong path (e.g. ``kb/design``
instead of ``design/modules.md``) because the path was only mentioned once, in
prose, inside a dense multi-line task description. The banner puts the path in
a dedicated high-visibility block at the top of every prompt.
"""

from __future__ import annotations

from pathlib import Path

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole
from agora.fleet.agent_runtime import AgentRuntime, build_output_path_banner
from agora.fleet.inner_tools import ToolContext
from agora.fleet.llm_adapter import LLMResponse
from agora.fleet.stage_runner import Stage, StagedTask, StageRunner
from tests.conftest import FakeLLM


def _passing_spec() -> Specification:
    return Specification(
        postconditions=(make_predicate("ok", "", lambda _c: (True, "")),),
        description="",
    )


# ------------------------------------------------------- build_output_path_banner


def test_banner_contains_the_path_verbatim() -> None:
    banner = build_output_path_banner("design/modules.md")
    assert "design/modules.md" in banner
    assert "PATH CONSTANT" in banner
    assert "REQUIRED OUTPUT PATH" in banner


def test_banner_warns_against_wrapping_paths() -> None:
    banner = build_output_path_banner("bot.py")
    assert "bot.py" in banner
    # The instruction must explicitly discourage wrapping.
    assert "extra director" in banner.lower() or "do not wrap" in banner.lower()


# ------------------------------------------------------------ AgentRuntime prompt


async def test_execute_task_prompt_includes_banner_when_output_path_set(
    tmp_path: Path, fake_matrix_client
) -> None:
    captured: list[list[dict]] = []

    class CapturingLLM(FakeLLM):
        async def complete(self, *args, **kwargs):
            captured.append(list(kwargs.get("messages") or []))
            return await super().complete(*args, **kwargs)

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
    )
    identity = AgentIdentity(
        agent_id="w",
        room_id=agent_room,
        config=AgentConfig(name="w", role=AgentRole.IMPLEMENTER),
    )
    runtime = AgentRuntime(
        llm=CapturingLLM([LLMResponse(content="done"), LLMResponse(content="[]")]),
        matrix_client=fake_matrix_client,
        tool_context=ctx,
    )
    task = Task(
        id="t",
        spec=_passing_spec(),
        description="do the thing",
        agent_id="w",
        output_path="design/modules.md",
    )
    await runtime.execute_task(task, identity)

    assert captured, "LLM was never called"
    first_user = captured[0][0]["content"]
    assert "REQUIRED OUTPUT PATH" in first_user
    assert "design/modules.md" in first_user
    # Banner should come BEFORE the free-form description so weak models see
    # the path before they get lost in prose.
    banner_idx = first_user.index("REQUIRED OUTPUT PATH")
    desc_idx = first_user.index("do the thing")
    assert banner_idx < desc_idx


async def test_execute_task_omits_banner_when_output_path_empty(
    tmp_path: Path, fake_matrix_client
) -> None:
    captured: list[list[dict]] = []

    class CapturingLLM(FakeLLM):
        async def complete(self, *args, **kwargs):
            captured.append(list(kwargs.get("messages") or []))
            return await super().complete(*args, **kwargs)

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
    )
    identity = AgentIdentity(
        agent_id="w",
        room_id=agent_room,
        config=AgentConfig(name="w", role=AgentRole.TESTER),
    )
    runtime = AgentRuntime(
        llm=CapturingLLM([LLMResponse(content="done"), LLMResponse(content="[]")]),
        matrix_client=fake_matrix_client,
        tool_context=ctx,
    )
    task = Task(id="t", spec=_passing_spec(), description="verify-only", agent_id="w")
    await runtime.execute_task(task, identity)
    first_user = captured[0][0]["content"]
    assert "REQUIRED OUTPUT PATH" not in first_user


# --------------------------------------------------------- StageRunner prompt


async def test_stage_runner_injects_banner_into_each_stage(
    tmp_path: Path, fake_matrix_client
) -> None:
    captured: list[list[dict]] = []

    class CapturingLLM(FakeLLM):
        async def complete(self, *args, **kwargs):
            captured.append(list(kwargs.get("messages") or []))
            return await super().complete(*args, **kwargs)

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
    )
    identity = AgentIdentity(
        agent_id="w",
        room_id=agent_room,
        config=AgentConfig(name="w", role=AgentRole.IMPLEMENTER),
    )
    runtime = AgentRuntime(
        llm=CapturingLLM(
            [
                LLMResponse(content="s1 done"),
                LLMResponse(content="s2 done"),
            ]
        ),
        matrix_client=fake_matrix_client,
        tool_context=ctx,
    )
    staged = StagedTask(
        task=Task(
            id="t",
            spec=_passing_spec(),
            description="build the thing",
            agent_id="w",
            output_path="bot.py",
        ),
        stages=[
            Stage(instruction="stage one", max_iterations=1),
            Stage(instruction="stage two", max_iterations=1),
        ],
    )
    await StageRunner(runtime).execute_staged_task(staged, identity)

    assert len(captured) == 2
    for call_msgs in captured:
        first_user = call_msgs[0]["content"]
        assert "REQUIRED OUTPUT PATH" in first_user
        assert "bot.py" in first_user


# ---------------------------------------------------- write_file soft-warning


async def test_write_file_warns_on_path_mismatch(
    tmp_path: Path, fake_matrix_client, caplog
) -> None:
    import logging

    from agora.fleet.inner_tools import _make_write

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=agent_room,
        expected_output_path="bot.py",
    )
    writer = _make_write(ctx)
    with caplog.at_level(logging.WARNING, logger="agora.fleet.inner_tools"):
        result = await writer({"path": "kb/wrong_place.md", "content": "x"})
    assert result.startswith("wrote")
    assert any("path mismatch" in rec.message for rec in caplog.records)


async def test_write_file_silent_when_path_matches(
    tmp_path: Path, fake_matrix_client, caplog
) -> None:
    import logging

    from agora.fleet.inner_tools import _make_write

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=agent_room,
        expected_output_path="bot.py",
    )
    writer = _make_write(ctx)
    with caplog.at_level(logging.WARNING, logger="agora.fleet.inner_tools"):
        await writer({"path": "bot.py", "content": "x = 1"})
    assert not any("path mismatch" in rec.message for rec in caplog.records)


async def test_write_file_silent_when_no_expected_path(
    tmp_path: Path, fake_matrix_client, caplog
) -> None:
    import logging

    from agora.fleet.inner_tools import _make_write

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=agent_room,
    )
    writer = _make_write(ctx)
    with caplog.at_level(logging.WARNING, logger="agora.fleet.inner_tools"):
        await writer({"path": "anywhere.md", "content": "x"})
    assert not any("path mismatch" in rec.message for rec in caplog.records)
