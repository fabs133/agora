"""Tests for the micro-stage task runner."""

from __future__ import annotations

from pathlib import Path

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole
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


async def test_staged_task_populates_artifact_capture_on_failure(
    tmp_path: Path, fake_matrix_client
) -> None:
    """Regression (S4): staged tasks build their OWN TaskResult, so the near-miss
    capture must fire on this path — it was 0/45 in v3.0 because the capture lived
    only in AgentRuntime.execute_task, which staged (probe) tasks never hit."""
    responses = [
        LLMResponse(
            content="",
            tool_calls=(tool_call("write_file", {"path": "out/x.txt", "content": "WRONG"}),),
        ),
        LLMResponse(content="done"),
        LLMResponse(content="[]"),
    ]
    runtime, _ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)
    fail_spec = Specification(
        postconditions=(make_predicate("nope", "", lambda _c: (False, "forced")),),
        description="",
    )
    staged = StagedTask(
        task=Task(id="t", spec=fail_spec, description="w", agent_id="w", output_path="out/x.txt"),
        stages=[Stage(instruction="write out/x.txt", max_iterations=3)],
    )
    result = await StageRunner(runtime).execute_staged_task(staged, identity)
    assert result.success is False
    assert result.artifact_capture is not None
    assert result.artifact_capture["path"] == "out/x.txt"
    assert result.artifact_capture["text"] == "WRONG"


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


# --------------------------------------------------------------- F13 invariant

def _mk_manifest(names: list[str]) -> list[dict]:
    return [{"name": n, "input_schema": {}} for n in names]


def test_overwrite_guard_hides_write_file_when_edit_family_present() -> None:
    """(b) unrestricted seat + existing output -> write_file hidden (v2.4)."""
    from agora.fleet.agent_runtime import _apply_overwrite_guard

    tools = _mk_manifest(["read_file", "write_file", "edit_file_replace",
                          "add_function", "mark_complete"])
    out, hid = _apply_overwrite_guard(tools, output_has_content=True)
    names = {t["name"] for t in out}
    assert hid is True
    assert "write_file" not in names
    assert "edit_file_replace" in names  # edit family remains the write path


def test_overwrite_guard_keeps_write_file_when_it_is_the_only_mutation_tool() -> None:
    """(a) allowlisted seat (no edit/AST family) + existing output -> write_file
    KEPT: hiding it would leave zero file-mutation affordances (F13)."""
    from agora.fleet.agent_runtime import _apply_overwrite_guard

    tools = _mk_manifest(["read_file", "write_file", "list_directory", "mark_complete"])
    out, hid = _apply_overwrite_guard(tools, output_has_content=True)
    names = {t["name"] for t in out}
    assert hid is False
    assert "write_file" in names


def test_overwrite_guard_noop_when_output_empty() -> None:
    from agora.fleet.agent_runtime import _apply_overwrite_guard

    tools = _mk_manifest(["read_file", "write_file", "list_directory", "mark_complete"])
    out, hid = _apply_overwrite_guard(tools, output_has_content=False)
    assert hid is False and out is tools


def test_overwrite_guard_property_always_leaves_a_mutation_affordance() -> None:
    """(c) property: a manifest that starts with >=1 mutation tool always keeps
    >=1 after the guard fires — a task with an output_path is never left unable
    to modify it."""
    from agora.fleet.agent_runtime import _WRITE_TOOL_NAMES, _apply_overwrite_guard

    for manifest in (
        ["read_file", "write_file", "list_directory", "mark_complete"],      # allowlisted seat
        ["read_file", "write_file", "edit_file_replace", "add_function"],     # unrestricted seat
        ["read_file", "write_file"],                                          # write_file only
        ["read_file", "add_function", "mark_complete"],                       # AST-only, no write_file
    ):
        out, _ = _apply_overwrite_guard(_mk_manifest(manifest), output_has_content=True)
        assert any(t["name"] in _WRITE_TOOL_NAMES for t in out), manifest


async def test_allowlisted_seat_keeps_write_file_on_existing_output(
    tmp_path: Path, fake_matrix_client
) -> None:
    """F13 end-to-end: the impl-seat allowlist drops the edit/AST family, so on
    an EXISTING output file the guard must keep write_file — else (run 1.4) the
    seat has no way to modify it."""
    from dataclasses import replace

    target = tmp_path / "core.py"
    target.write_text("def handle_message(self, message): ...", encoding="utf-8")
    responses = [LLMResponse(content="reading first")]
    runtime, _ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)
    identity = AgentIdentity(
        agent_id=identity.agent_id, room_id=identity.room_id,
        config=replace(identity.config,
                       allowed_tools=("read_file", "write_file", "list_directory", "mark_complete")),
    )
    staged = StagedTask(
        task=Task(id="t1", spec=_file_exists_spec("core.py"), description="edit",
                  agent_id="w", output_path="core.py"),
        stages=[Stage(instruction="Edit core.py.", max_iterations=1)],
    )
    await StageRunner(runtime).execute_staged_task(staged, identity)
    tool_names = {t["name"] for t in (runtime._llm.calls[0]["tools"] or [])}
    assert "write_file" in tool_names      # kept — only mutation affordance
    assert "add_function" not in tool_names  # allowlist dropped the edit family


async def test_manifest_delta_logging_emits_both_lines(
    tmp_path: Path, fake_matrix_client, caplog
) -> None:
    """Item 2: the allowlist filter and the write_file hide each log one INFO
    manifest-delta line. Here an allowlist that KEEPS an edit tool triggers both
    (filter drops the rest; guard then hides write_file since the edit tool
    remains as the write path)."""
    import logging
    from dataclasses import replace

    target = tmp_path / "core.py"
    target.write_text("existing", encoding="utf-8")
    responses = [LLMResponse(content="x")]
    runtime, _ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)
    identity = AgentIdentity(
        agent_id=identity.agent_id, room_id=identity.room_id,
        config=replace(identity.config,
                       allowed_tools=("read_file", "write_file", "edit_file_replace",
                                      "list_directory", "mark_complete")),
    )
    staged = StagedTask(
        task=Task(id="t1", spec=_file_exists_spec("core.py"), description="edit",
                  agent_id="w", output_path="core.py"),
        stages=[Stage(instruction="Edit core.py.", max_iterations=1)],
    )
    with caplog.at_level(logging.INFO):
        await StageRunner(runtime).execute_staged_task(staged, identity)
    text = caplog.text
    assert "manifest: filtered" in text and "(allowlist)" in text
    assert "manifest: hid write_file (overwrite guard)" in text


# --------------------------------------------------------------- S7 reasoning-salvage

async def test_s7_salvage_fires_once_and_carries_draft_verbatim(
    tmp_path: Path, fake_matrix_client
) -> None:
    """S7: a reasoning-only turn (0 tool calls, empty content, non-empty thinking)
    triggers ONE salvage re-prompt carrying the draft verbatim; the model then
    emits the call. Provenance: salvages_used=1, turns_reasoning_only=1."""
    draft = "## Identity\n\n<<DRAFT-MARKER-77>>\n\n## How to run / test"
    responses = [
        LLMResponse(content="", tool_calls=(), thinking=draft),  # reasoning-only turn
        LLMResponse(content="", tool_calls=(
            tool_call("write_file", {"path": "out.md", "content": "x"}),)),  # emits after salvage
    ]
    runtime, ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)
    ctx.salvage_budget = 1
    staged = StagedTask(
        task=Task(id="t1", spec=_file_exists_spec("out.md"), description="w",
                  agent_id="w", output_path="out.md"),
        stages=[Stage(instruction="Write out.md.", max_iterations=4)],
    )
    await StageRunner(runtime).execute_staged_task(staged, identity)
    assert runtime._salvages_used == 1
    assert runtime._turns_reasoning_only == 1
    # the salvage re-prompt (injected before the 2nd call) carries the draft verbatim
    second = "\n".join(
        m["content"] for m in runtime._llm.calls[1]["messages"] if isinstance(m.get("content"), str)
    )
    assert "<<DRAFT-MARKER-77>>" in second
    assert "Emit the required tool call now" in second


async def test_s7_salvage_budget_zero_is_construct_nothing(
    tmp_path: Path, fake_matrix_client
) -> None:
    """salvage_budget=0: a reasoning-only turn injects NOTHING (byte-identical to
    pre-S7). The counter still records the derailment (provenance-only)."""
    draft = "some analysis <<NOPE>>"
    responses = [LLMResponse(content="", tool_calls=(), thinking=draft)]
    runtime, ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)
    ctx.salvage_budget = 0
    staged = StagedTask(
        task=Task(id="t1", spec=_file_exists_spec("out.md"), description="w",
                  agent_id="w", output_path="out.md"),
        stages=[Stage(instruction="Write out.md.", max_iterations=3)],
    )
    await StageRunner(runtime).execute_staged_task(staged, identity)
    assert runtime._salvages_used == 0
    assert runtime._turns_reasoning_only == 1  # provenance records the gap
    assert len(runtime._llm.calls) == 1  # no salvage re-prompt => no extra call
    injected = "\n".join(
        m["content"] for c in runtime._llm.calls for m in c["messages"]
        if isinstance(m.get("content"), str)
    )
    assert "Emit the required tool call now" not in injected  # nothing constructed


async def test_s7_does_not_fire_when_content_present(
    tmp_path: Path, fake_matrix_client
) -> None:
    """The trigger is EXACT: content present (even with thinking) is not a
    reasoning-only turn — no salvage, no counter."""
    responses = [LLMResponse(content="here is text", tool_calls=(), thinking="draft")]
    runtime, ctx, identity = await _make_runtime(tmp_path, fake_matrix_client, responses)
    ctx.salvage_budget = 1
    staged = StagedTask(
        task=Task(id="t1", spec=_always_pass_spec(), description="w", agent_id="w"),
        stages=[Stage(instruction="Do it.", max_iterations=2)],
    )
    await StageRunner(runtime).execute_staged_task(staged, identity)
    assert runtime._salvages_used == 0
    assert runtime._turns_reasoning_only == 0  # content present => not the condition


def test_extract_thinking_blocks_pairs_with_strip() -> None:
    from agora.fleet.llm_adapter import _extract_thinking_blocks, _strip_thinking_blocks

    text = "before<think>REASONING HERE</think>after"
    assert _extract_thinking_blocks(text) == "REASONING HERE"
    assert _strip_thinking_blocks(text) == "beforeafter"
    # unterminated open: capture to end
    assert "tail" in _extract_thinking_blocks("x<think>tail")
    # no blocks
    assert _extract_thinking_blocks("plain content") == ""


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
