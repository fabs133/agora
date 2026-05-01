"""Tests for the micro-stage task runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus
from agora.fleet.agent_runtime import AgentRuntime
from agora.fleet.inner_tools import ToolContext
from agora.fleet.llm_adapter import LLMResponse
from agora.fleet.stage_runner import Stage, StagedTask, StageRunner
from tests.conftest import FakeLLM, tool_call


def _always_pass_spec(name: str = "ok") -> Specification:
    return Specification(
        postconditions=(make_predicate(name, "", lambda _c: (True, "")),),
        description="",
    )


def _file_exists_spec(rel: str) -> Specification:
    def check(ctx):
        work_dir = ctx.get("work_dir")
        return (
            bool(work_dir) and (Path(work_dir) / rel).is_file(),
            f"{rel} not written",
        )

    return Specification(
        postconditions=(make_predicate("file_exists", rel, check),),
        description="",
    )


async def _make_runtime(
    tmp_path: Path, fake_matrix_client, responses, *, auto_hooks: bool = False
) -> tuple[AgentRuntime, ToolContext, AgentIdentity]:
    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        auto_hooks_enabled=auto_hooks,
    )
    identity = AgentIdentity(
        agent_id="w",
        room_id=agent_room,
        config=AgentConfig(name="w", role=AgentRole.IMPLEMENTER),
    )
    runtime = AgentRuntime(
        llm=FakeLLM(responses),
        matrix_client=fake_matrix_client,
        tool_context=ctx,
    )
    return runtime, ctx, identity


async def test_single_stage_completes_and_runs_postconditions(
    tmp_path: Path, fake_matrix_client
) -> None:
    """One-stage task writes a file, postcondition sees it."""
    responses = [
        LLMResponse(
            content="",
            tool_calls=(tool_call("write_file", {"path": "hello.txt", "content": "hi"}),),
        ),
        LLMResponse(content="done"),
        # Reflection pass (called by execute_task, not _run_loop, but StageRunner
        # also exercises _extract_learnings indirectly through the postcondition path).
        LLMResponse(content="[]"),
    ]
    runtime, ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)
    staged = StagedTask(
        task=Task(
            id="t1",
            spec=_file_exists_spec("hello.txt"),
            description="write",
            agent_id="w",
        ),
        stages=[Stage(instruction="Write hello.txt with 'hi'.", max_iterations=3)],
    )

    result = await StageRunner(runtime).execute_staged_task(staged, identity)
    assert result.success is True
    assert (tmp_path / "hello.txt").is_file()


async def test_multi_stage_gets_fresh_message_history(
    tmp_path: Path, fake_matrix_client
) -> None:
    """Each stage starts with a fresh `messages` list (no accumulated bloat)."""
    # Stage 1 writes file A, Stage 2 writes file B. Each stage's LLM call
    # sequence is independent. FakeLLM returns the same sequence on each call
    # so both stages get "write then stop".
    responses = [
        LLMResponse(
            content="",
            tool_calls=(tool_call("write_file", {"path": "a.txt", "content": "A"}),),
        ),
        LLMResponse(content="stage1 done"),
        LLMResponse(
            content="",
            tool_calls=(tool_call("write_file", {"path": "b.txt", "content": "B"}),),
        ),
        LLMResponse(content="stage2 done"),
    ]
    runtime, ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)

    staged = StagedTask(
        task=Task(
            id="t2",
            spec=_file_exists_spec("b.txt"),
            description="multi",
            agent_id="w",
        ),
        stages=[
            Stage(instruction="Write a.txt with 'A'.", max_iterations=3),
            Stage(instruction="Write b.txt with 'B'.", max_iterations=3),
        ],
    )
    result = await StageRunner(runtime).execute_staged_task(staged, identity)
    assert result.success is True
    assert (tmp_path / "a.txt").is_file()
    assert (tmp_path / "b.txt").is_file()


async def test_stage_validation_failure_aborts_task(
    tmp_path: Path, fake_matrix_client
) -> None:
    """When a stage's validation fails, the task fails without running later stages."""

    def always_fail(_ctx):
        return False, "always fails"

    responses = [
        LLMResponse(
            content="",
            tool_calls=(tool_call("write_file", {"path": "a.txt", "content": "A"}),),
        ),
        LLMResponse(content="stage1"),
    ]
    runtime, ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)
    staged = StagedTask(
        task=Task(
            id="t3",
            spec=_always_pass_spec(),
            description="val",
            agent_id="w",
        ),
        stages=[
            Stage(
                instruction="Write a.txt.",
                max_iterations=2,
                validation=always_fail,
                name="first",
            ),
            Stage(instruction="Should never run.", max_iterations=2, name="second"),
        ],
    )
    result = await StageRunner(runtime).execute_staged_task(staged, identity)
    assert result.success is False
    names = [n for n, _, _ in result.postcondition_results]
    assert "stage_first" in names


async def test_stage_preloads_context_files_into_user_message(
    tmp_path: Path, fake_matrix_client
) -> None:
    """`context_files` are embedded in the stage's user message verbatim."""
    (tmp_path / "design.md").write_text("DESIGN BODY", encoding="utf-8")

    captured_messages: list[list[dict]] = []

    class CapturingLLM(FakeLLM):
        async def complete(self, *args, **kwargs):
            captured_messages.append(list(kwargs.get("messages") or []))
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
        llm=CapturingLLM([LLMResponse(content="ok")]),
        matrix_client=fake_matrix_client,
        tool_context=ctx,
    )
    staged = StagedTask(
        task=Task(
            id="t4",
            spec=_always_pass_spec(),
            description="ctx",
            agent_id="w",
        ),
        stages=[
            Stage(
                instruction="Use the design doc.",
                context_files=("design.md",),
                max_iterations=2,
            ),
        ],
    )
    await StageRunner(runtime).execute_staged_task(staged, identity)
    assert captured_messages, "LLM was never called"
    first_user = captured_messages[0][0]["content"]
    assert "DESIGN BODY" in first_user
    assert "design.md" in first_user


async def test_stage_auto_hooks_fire_after_write(
    tmp_path: Path, fake_matrix_client
) -> None:
    """With ``auto_hooks_enabled``, writing a broken .py triggers check_python."""
    bad_py = "def broken(\n"  # SyntaxError
    responses = [
        LLMResponse(
            content="",
            tool_calls=(tool_call("write_file", {"path": "bad.py", "content": bad_py}),),
        ),
        LLMResponse(content="stopped"),
    ]
    runtime, ctx, identity = await _make_runtime(
        tmp_path, fake_matrix_client, responses, auto_hooks=True
    )
    staged = StagedTask(
        task=Task(
            id="t5",
            spec=_always_pass_spec(),
            description="hook",
            agent_id="w",
        ),
        stages=[Stage(instruction="Write bad.py.", max_iterations=2)],
    )
    await StageRunner(runtime).execute_staged_task(staged, identity)
    # The auto-hook should have created a synthetic completion entry.
    assert ctx.completions, "auto-hook never synthesized mark_complete"
    assert ctx.completions[0].get("auto") is True


async def test_stage_hides_write_file_when_output_path_has_content(
    tmp_path: Path, fake_matrix_client
) -> None:
    """Per-turn filter: if the task's output_path file already has content,
    write_file is dropped from the LLM's tool manifest so the 7B can't cycle
    through write_file → guard ERROR → retry. Other file tools stay visible."""
    # Pre-populate the output file to simulate a prior retry's partial write.
    target = tmp_path / "hello.txt"
    target.write_text("existing content that shouldn't be clobbered", encoding="utf-8")

    # One LLM turn that just produces text (no tool calls). We don't need the
    # model to actually call anything — we only need to capture what tools it
    # *saw* in the manifest on that call.
    responses = [LLMResponse(content="nothing to do — file already exists")]
    runtime, ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)
    staged = StagedTask(
        task=Task(
            id="t1",
            spec=_file_exists_spec("hello.txt"),
            description="edit",
            agent_id="w",
            output_path="hello.txt",
        ),
        stages=[Stage(instruction="Edit hello.txt.", max_iterations=1)],
    )
    await StageRunner(runtime).execute_staged_task(staged, identity)

    # FakeLLM records every .complete() call. Check the tools list passed
    # on the first turn.
    assert runtime._llm.calls, "expected at least one LLM call"
    tool_names = {t["name"] for t in (runtime._llm.calls[0]["tools"] or [])}
    assert "write_file" not in tool_names, (
        f"write_file should be hidden when output_path exists; got {tool_names}"
    )
    assert "edit_file_replace" in tool_names
    # Original content preserved (no write_file → no clobber).
    assert target.read_text() == "existing content that shouldn't be clobbered"


async def test_stage_keeps_write_file_when_output_path_empty(
    tmp_path: Path, fake_matrix_client
) -> None:
    """Negative case: when the output file doesn't exist, write_file stays
    visible. This is the normal create-a-file flow."""
    responses = [LLMResponse(content="ok")]
    runtime, ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)
    staged = StagedTask(
        task=Task(
            id="t1",
            spec=_always_pass_spec(),
            description="create",
            agent_id="w",
            output_path="new.txt",
        ),
        stages=[Stage(instruction="Create new.txt.", max_iterations=1)],
    )
    await StageRunner(runtime).execute_staged_task(staged, identity)
    tool_names = {t["name"] for t in (runtime._llm.calls[0]["tools"] or [])}
    assert "write_file" in tool_names


async def test_stage_token_usage_aggregates_across_stages(
    tmp_path: Path, fake_matrix_client
) -> None:
    responses = [
        LLMResponse(content="one", usage={"input_tokens": 10, "output_tokens": 5}),
        LLMResponse(content="two", usage={"input_tokens": 20, "output_tokens": 8}),
    ]
    runtime, ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)
    staged = StagedTask(
        task=Task(
            id="t6",
            spec=_always_pass_spec(),
            description="tok",
            agent_id="w",
        ),
        stages=[
            Stage(instruction="one", max_iterations=1),
            Stage(instruction="two", max_iterations=1),
        ],
    )
    result = await StageRunner(runtime).execute_staged_task(staged, identity)
    assert result.token_usage["input_tokens"] == 30
    assert result.token_usage["output_tokens"] == 13
