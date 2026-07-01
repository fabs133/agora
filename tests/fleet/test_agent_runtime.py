from pathlib import Path

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus
from agora.fleet.agent_runtime import AgentRuntime, _parse_learnings, _ToolCallStats
from agora.fleet.inner_tools import ToolContext
from agora.fleet.llm_adapter import LLMResponse
from agora.matrix.events import LEARNING_EVENT
from tests.conftest import FakeLLM, tool_call


def test_tool_call_stats_reconcile_to_call_counts() -> None:
    """structured + text_fallback == total, in the same (call) unit."""
    stats = _ToolCallStats()
    # Turn 0: two structured calls.
    stats.note_turn(
        [tool_call("write_file", {}), tool_call("mark_complete", {})],
        ["ok", "ok"],
        from_text_fallback=False,
        iteration=0,
    )
    # Turn 1: one more structured call.
    stats.note_turn(
        [tool_call("read_file", {})], ["ok"], from_text_fallback=False, iteration=1
    )
    # Turn 2: three calls parsed out of prose (one fallback fire).
    stats.note_turn(
        [tool_call("a", {}), tool_call("b", {}), tool_call("c", {})],
        ["ok", "ok", "ok"],
        from_text_fallback=True,
        iteration=2,
    )
    assert stats.total == 6
    assert stats.structured == 3
    assert stats.text_fallback == 3
    assert stats.structured + stats.text_fallback == stats.total
    # Side channel: one fallback turn, first at iteration 2.
    assert stats.turns_with_text_fallback == 1
    assert stats.first_text_fallback_iteration == 2


def test_tool_call_stats_overlap_counters() -> None:
    """malformed / unknown_name are subsets, not part of the reconciliation sum."""
    stats = _ToolCallStats()
    stats.note_turn(
        [tool_call("write_file", {}), tool_call("bogus", {})],
        ["ok", "ERROR: unknown tool 'bogus'"],
        from_text_fallback=False,
        iteration=0,
    )
    stats.note_turn(
        [tool_call("write_file", {})],
        ["ERROR: tool write_file raised: bad args"],
        from_text_fallback=False,
        iteration=1,
    )
    assert stats.total == 3
    assert stats.structured == 3  # both turns native
    assert stats.text_fallback == 0
    assert stats.unknown_name == 1
    assert stats.malformed == 1


def test_tool_call_stats_apply_to_taskresult() -> None:
    from agora.fleet.agent_runtime import TaskResult

    stats = _ToolCallStats()
    stats.note_turn(
        [tool_call("x", {})], ["ok"], from_text_fallback=True, iteration=0
    )
    tr = stats.apply_to(TaskResult(task_id="t", success=True, output=""))
    assert tr.tool_calls_total == 1
    assert tr.tool_calls_text_fallback == 1
    assert tr.tool_calls_structured == 0
    assert tr.turns_with_text_fallback == 1
    assert tr.first_text_fallback_iteration == 0


def _identity(name: str = "impl") -> AgentIdentity:
    return AgentIdentity(
        agent_id=f"@{name}:agora.local",
        room_id=f"!{name}:agora.local",
        config=AgentConfig(name=name, role=AgentRole.IMPLEMENTER, instructions="do work"),
    )


def _spec_requires_artifact() -> Specification:
    def has_artifact(ctx):
        arts = ctx.get("artifacts") or []
        return (bool(arts), "no artifacts recorded")

    return Specification(
        postconditions=(make_predicate("has_artifact", "produces ≥1 file", has_artifact),),
        description="must produce at least one artifact",
    )


def _task(tid: str = "t1", spec: Specification | None = None) -> Task:
    return Task(
        id=tid,
        spec=spec or Specification(),
        description="Write hello.txt containing 'hi'",
        status=TaskStatus.PENDING,
    )


async def _prepare(tmp_path: Path, fake_matrix_client, llm: FakeLLM):
    identity = _identity()
    # Pre-create the agent's identity room so learnings can be stored.
    await fake_matrix_client.create_room(name="agent:impl", topic="impl")
    room_id = next(iter(fake_matrix_client.rooms.keys()))
    identity = AgentIdentity(
        agent_id=identity.agent_id,
        room_id=room_id,
        config=identity.config,
    )
    # Project room
    proj_room = await fake_matrix_client.create_room(name="proj", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=room_id,
        project_room_id=proj_room,
    )
    return AgentRuntime(llm=llm, matrix_client=fake_matrix_client, tool_context=ctx), identity, ctx


async def test_simple_task_no_tools(tmp_path, fake_matrix_client) -> None:
    llm = FakeLLM(
        [
            LLMResponse(content="nothing to do"),
            LLMResponse(content="[]"),  # reflection returns empty list
        ]
    )
    runtime, identity, _ = await _prepare(tmp_path, fake_matrix_client, llm)
    result = await runtime.execute_task(_task(), identity)
    assert result.iterations == 1
    assert result.success is True  # no postconditions → vacuously true
    assert result.output == "nothing to do"


async def test_task_with_tool_calls(tmp_path, fake_matrix_client) -> None:
    llm = FakeLLM(
        [
            LLMResponse(
                content="",
                tool_calls=(
                    tool_call("write_file", {"path": "hello.txt", "content": "hi"}),
                ),
            ),
            LLMResponse(
                content="",
                tool_calls=(tool_call("mark_complete", {"summary": "done", "artifacts": ["hello.txt"]}),),
            ),
            LLMResponse(content="all done"),
            LLMResponse(
                content='[{"category":"pattern","content":"write then complete","confidence":0.8}]'
            ),
        ]
    )
    runtime, identity, ctx = await _prepare(tmp_path, fake_matrix_client, llm)
    result = await runtime.execute_task(_task(spec=_spec_requires_artifact()), identity)

    assert (tmp_path / "hello.txt").read_text() == "hi"
    assert "hello.txt" in result.artifacts
    assert result.success is True
    # Learning persisted to the agent identity room
    timeline = await fake_matrix_client.get_room_timeline(ctx.agent_room_id)
    assert any(ev["type"] == LEARNING_EVENT for ev in timeline)


async def test_postcondition_failure_marks_failed(tmp_path, fake_matrix_client) -> None:
    llm = FakeLLM(
        [
            LLMResponse(content="did nothing"),
            LLMResponse(content="[]"),
        ]
    )
    runtime, identity, _ = await _prepare(tmp_path, fake_matrix_client, llm)
    result = await runtime.execute_task(_task(spec=_spec_requires_artifact()), identity)
    assert result.success is False
    assert result.postcondition_results == [("has_artifact", False, "no artifacts recorded")]


async def test_max_iterations_breaker(tmp_path, fake_matrix_client) -> None:
    # Every response has a tool call → runtime must stop at max_iterations.
    infinite = LLMResponse(
        content="", tool_calls=(tool_call("report_progress", {"message": "still going"}),)
    )
    llm = FakeLLM([infinite] * 20 + [LLMResponse(content="[]")])
    runtime, identity, _ = await _prepare(tmp_path, fake_matrix_client, llm)
    result = await runtime.execute_task(_task(), identity, max_iterations=3)
    assert result.iterations == 3


async def test_unknown_tool_returns_error(tmp_path, fake_matrix_client) -> None:
    llm = FakeLLM(
        [
            LLMResponse(content="", tool_calls=(tool_call("bogus_tool", {}),)),
            LLMResponse(content="stopping"),
            LLMResponse(content="[]"),
        ]
    )
    runtime, identity, _ = await _prepare(tmp_path, fake_matrix_client, llm)
    result = await runtime.execute_task(_task(), identity)
    # The error is returned to the model; then it stops.
    assert result.iterations == 2


def test_parse_learnings_handles_fenced_json() -> None:
    raw = '```json\n[{"category":"pattern","content":"x","confidence":0.7}]\n```'
    learnings = _parse_learnings(raw, task_ref="t1")
    assert len(learnings) == 1
    assert learnings[0].content == "x"


def test_parse_learnings_ignores_bad_category() -> None:
    raw = '[{"category":"nope","content":"x"}]'
    assert _parse_learnings(raw, task_ref="t1") == []


def test_parse_learnings_empty_input() -> None:
    assert _parse_learnings("", task_ref="t1") == []
    assert _parse_learnings("no json here", task_ref="t1") == []
