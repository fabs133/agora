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


def _task_out(tid: str = "t", spec: Specification | None = None) -> Task:
    return Task(
        id=tid, spec=spec or Specification(), description="write out/x.txt",
        status=TaskStatus.PENDING, output_path="out/x.txt",
    )


async def test_nudge_budget_zero_is_v2_behavior(tmp_path, fake_matrix_client) -> None:
    """budget=0 ⇒ a 0-tool-call turn breaks immediately, no nudge injected."""
    llm = FakeLLM([LLMResponse(content="I will write it now", tool_calls=()),
                   LLMResponse(content="[]")])
    runtime, identity, ctx = await _prepare(tmp_path, fake_matrix_client, llm)
    ctx.nudge_budget = 0
    result = await runtime.execute_task(_task_out(), identity)
    assert result.iterations == 1  # broke on turn 1, no extra turn
    assert not any(
        "Not complete: expected output" in str(m.get("content"))
        for call in llm.calls for m in call["messages"]
    )


async def test_nudge_budget_one_injects_exactly_once(tmp_path, fake_matrix_client) -> None:
    """budget=1 ⇒ nudge once (output unwritten), run one more turn, then terminate."""
    llm = FakeLLM([LLMResponse(content="narrating 1", tool_calls=()),
                   LLMResponse(content="narrating 2", tool_calls=()),
                   LLMResponse(content="[]")])
    runtime, identity, ctx = await _prepare(tmp_path, fake_matrix_client, llm)
    ctx.nudge_budget = 1
    result = await runtime.execute_task(_task_out(), identity)
    assert result.iterations == 2  # nudged once → a 2nd turn → then break
    # the corrective turn reached the model on the 2nd call, naming the output
    injected = [
        m for m in llm.calls[1]["messages"]
        if "Not complete: expected output out/x.txt has not been written" in str(m.get("content"))
    ]
    assert len(injected) == 1


async def test_nudge_skipped_when_output_present(tmp_path, fake_matrix_client) -> None:
    """No nudge when the expected output already exists (postconditions-met proxy),
    even with budget available."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x.txt").write_text("done")
    llm = FakeLLM([LLMResponse(content="done", tool_calls=()), LLMResponse(content="[]")])
    runtime, identity, ctx = await _prepare(tmp_path, fake_matrix_client, llm)
    ctx.nudge_budget = 3
    result = await runtime.execute_task(_task_out(), identity)
    assert result.iterations == 1  # output present → no nudge


_REVIEW_LINE = "Review the written content against the task."


def _count_readbacks(messages) -> int:
    """Count S6 read-back tool-result blocks in one message list."""
    n = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and _REVIEW_LINE in str(block.get("content", "")):
                    n += 1
        elif _REVIEW_LINE in str(content):
            n += 1
    return n


def _longest_history(llm) -> list:
    """The message list from the loop turn with the most accumulated history."""
    return max(llm.calls, key=lambda c: len(c["messages"]))["messages"]


async def test_review_budget_zero_constructs_nothing(tmp_path, fake_matrix_client) -> None:
    """budget=0 ⇒ a valid mark_complete injects no read-back — v3.2 behaviour."""
    llm = FakeLLM([
        LLMResponse(content="", tool_calls=(
            tool_call("write_file", {"path": "out/x.txt", "content": "hi\n"}),)),
        LLMResponse(content="", tool_calls=(tool_call("mark_complete", {"summary": "done"}),)),
        LLMResponse(content="wrapping up", tool_calls=()),
        LLMResponse(content="[]"),
    ])
    runtime, identity, ctx = await _prepare(tmp_path, fake_matrix_client, llm)
    ctx.review_budget = 0
    result = await runtime.execute_task(_task_out(), identity)
    assert all(_count_readbacks(c["messages"]) == 0 for c in llm.calls)
    assert result.reviews_used == 0
    assert result.post_review_action is None


async def test_review_fires_once_then_finalizes(tmp_path, fake_matrix_client) -> None:
    """budget=1 ⇒ review fires on the first valid mark_complete; the next valid
    mark_complete finalizes with NO second read-back (budget exhausted)."""
    llm = FakeLLM([
        LLMResponse(content="", tool_calls=(
            tool_call("write_file", {"path": "out/x.txt", "content": "hi\n"}),)),
        LLMResponse(content="", tool_calls=(tool_call("mark_complete", {"summary": "done"}),)),
        LLMResponse(content="", tool_calls=(tool_call("mark_complete", {"summary": "confirmed"}),)),
        LLMResponse(content="bye", tool_calls=()),
        LLMResponse(content="[]"),
    ])
    runtime, identity, ctx = await _prepare(tmp_path, fake_matrix_client, llm)
    ctx.review_budget = 1
    result = await runtime.execute_task(_task_out(), identity)
    assert result.reviews_used == 1
    assert result.post_review_action == "confirm"
    # Exactly one read-back reached the model, despite two valid mark_completes.
    assert _count_readbacks(_longest_history(llm)) == 1


async def test_review_readback_is_verbatim_with_byte_count(tmp_path, fake_matrix_client) -> None:
    """The injected read-back names the byte count and carries the file bytes
    verbatim (real newlines, no re-escaping)."""
    from agora.fleet.agent_runtime import _render_completion_readback

    (tmp_path / "out").mkdir()
    content = "line1\nline2\n"  # 12 bytes
    (tmp_path / "out" / "x.txt").write_bytes(content.encode("utf-8"))
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id="!a:agora.local",
        project_room_id="!p:agora.local",
    )
    ctx.written_files.append("out/x.txt")
    out = _render_completion_readback(ctx)
    assert "out/x.txt (12 bytes):" in out
    assert content in out  # verbatim, real 0x0a newlines
    assert out.rstrip().endswith("or revise the file first.")


async def test_review_revise_action_and_no_refire(tmp_path, fake_matrix_client) -> None:
    """A file edit after the review classifies as 'revise'; a subsequent valid
    mark_complete does NOT fire a second review (budget exhausted)."""
    llm = FakeLLM([
        LLMResponse(content="", tool_calls=(
            tool_call("write_file", {"path": "out/x.txt", "content": "bad\n"}),)),
        LLMResponse(content="", tool_calls=(tool_call("mark_complete", {"summary": "done"}),)),
        LLMResponse(content="", tool_calls=(
            tool_call("write_file", {"path": "out/x.txt", "content": "good\n", "force": True}),)),
        LLMResponse(content="", tool_calls=(tool_call("mark_complete", {"summary": "fixed"}),)),
        LLMResponse(content="done", tool_calls=()),
        LLMResponse(content="[]"),
    ])
    runtime, identity, ctx = await _prepare(tmp_path, fake_matrix_client, llm)
    ctx.review_budget = 1
    result = await runtime.execute_task(_task_out(), identity)
    assert result.reviews_used == 1
    assert result.post_review_action == "revise"
    assert _count_readbacks(_longest_history(llm)) == 1


async def test_artifact_capture_on_postcondition_failure(tmp_path, fake_matrix_client) -> None:
    """A task that writes its output but fails a postcondition captures the bytes."""
    def _always_fail(_ctx):
        return (False, "forced")

    spec = Specification(
        postconditions=(make_predicate("nope", "always fails", _always_fail),),
        description="x",
    )
    llm = FakeLLM([
        LLMResponse(content="", tool_calls=(tool_call("write_file", {"path": "out/x.txt", "content": "WRONG"}),)),
        LLMResponse(content="", tool_calls=(tool_call("mark_complete", {"summary": "done"}),)),
        LLMResponse(content="stop"),
        LLMResponse(content="[]"),
    ])
    runtime, identity, _ = await _prepare(tmp_path, fake_matrix_client, llm)
    result = await runtime.execute_task(_task_out(spec=spec), identity)
    assert result.success is False
    assert result.artifact_capture is not None
    assert result.artifact_capture["path"] == "out/x.txt"
    assert result.artifact_capture["text"] == "WRONG"
    assert result.artifact_capture["truncated"] is False


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
