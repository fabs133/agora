"""Declarative decision-stage coverage.

Covers:
- ``StageTemplate(kind="decision", ...)`` schema validation in flow.py.
- ``StageRunner._execute_decision_stage`` posts the question card, registers
  reactions, posts the poll, awaits resolution, writes the answer to disk.
- Three voting surfaces round-trip into ``control.resolve_decision``: poll
  click, emoji reaction, ``/agora decision <answer>`` chat command.
- Poll content follows the MSC3381 stable schema (no unstable dual-emit).

No live Matrix. No LLM calls. The decision stage bypasses the LLM entirely,
which makes it fully unit-testable with a fake client.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.contract import Specification, make_predicate
from agora.core.errors import AgoraError
from agora.core.flow import StageTemplate, load_flow
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus
from agora.fleet.agent_runtime import AgentRuntime
from agora.fleet.control import OrchestratorControl
from agora.fleet.inner_tools import ToolContext
from agora.fleet.stage_runner import StageRunner, StagedTask, _EMOJI_DIGITS


class _FakeMatrixClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def send_event(self, room_id: str, event_type: str, content: dict) -> str:
        self.sent.append((room_id, event_type, content))
        return f"$evt_{len(self.sent)}"


class _FakeLLM:
    """Decision stages never call the LLM — this is only here to satisfy AgentRuntime."""

    async def complete(self, *args, **kwargs):
        raise AssertionError("decision stage should not invoke the LLM")


def _make_runtime(tmp_path: Path) -> tuple[AgentRuntime, OrchestratorControl, _FakeMatrixClient]:
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
    return runtime, control, client


def _decision_staged_task(decision_id: str, options: list[str], output_path: str) -> StagedTask:
    from agora.fleet.stage_runner import Stage

    task = Task(
        id=f"t_{decision_id}",
        spec=Specification(
            postconditions=(
                make_predicate("_trivial", "always true", lambda _ctx: (True, "")),
            ),
            description="decision test task",
        ),
        description="decision test",
        agent_id="architect",
        status=TaskStatus.PENDING,
        output_path=output_path,
    )
    stage = Stage(
        name=f"ask_{decision_id}",
        max_iterations=0,
        kind="decision",
        decision_id=decision_id,
        question=f"Pick one for {decision_id}",
        options=tuple(options),
        output_path=output_path,
    )
    return StagedTask(task=task, stages=[stage])


# ---------------------------------------------------------------------- schema tests


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_decision_stage_schema_loads(tmp_path: Path):
    _write_yaml(
        tmp_path / "ok.yaml",
        {
            "version": "2.0",
            "name": "ok",
            "agents": [{"name": "a", "role": "architect"}],
            "task_graph": [
                {
                    "id": "ask",
                    "assigned_to": "a",
                    "description": "decision",
                    "output_path": "ans.txt",
                    "stages": [
                        {
                            "name": "ask_x",
                            "kind": "decision",
                            "decision_id": "x",
                            "question": "Which?",
                            "options": ["foo", "bar"],
                            "output_path": "ans.txt",
                        }
                    ],
                }
            ],
        },
    )
    flow = load_flow(tmp_path / "ok.yaml")
    stage = flow.task_graph[0].stages[0]
    assert stage.kind == "decision"
    assert stage.decision_id == "x"
    assert stage.question == "Which?"
    assert stage.options == ("foo", "bar")
    assert stage.output_path == "ans.txt"
    assert stage.instruction == ""


def test_decision_stage_requires_options_ge_2(tmp_path: Path):
    _write_yaml(
        tmp_path / "bad.yaml",
        {
            "version": "2.0",
            "name": "bad",
            "agents": [{"name": "a", "role": "architect"}],
            "task_graph": [
                {
                    "id": "ask",
                    "assigned_to": "a",
                    "description": "bad",
                    "stages": [
                        {
                            "name": "ask_x",
                            "kind": "decision",
                            "decision_id": "x",
                            "question": "?",
                            "options": ["only_one"],
                            "output_path": "ans.txt",
                        }
                    ],
                }
            ],
        },
    )
    with pytest.raises(AgoraError, match=r"≥ 2 options"):
        load_flow(tmp_path / "bad.yaml")


def test_decision_stage_rejects_instruction(tmp_path: Path):
    _write_yaml(
        tmp_path / "mix.yaml",
        {
            "version": "2.0",
            "name": "mix",
            "agents": [{"name": "a", "role": "architect"}],
            "task_graph": [
                {
                    "id": "ask",
                    "assigned_to": "a",
                    "description": "mixed",
                    "stages": [
                        {
                            "name": "ask_x",
                            "kind": "decision",
                            "decision_id": "x",
                            "question": "?",
                            "options": ["a", "b"],
                            "output_path": "ans.txt",
                            "instruction": "forbidden — mixing llm and decision modes",
                        }
                    ],
                }
            ],
        },
    )
    with pytest.raises(AgoraError, match="cannot declare an instruction"):
        load_flow(tmp_path / "mix.yaml")


def test_decision_stage_unknown_kind(tmp_path: Path):
    _write_yaml(
        tmp_path / "bad.yaml",
        {
            "version": "2.0",
            "name": "bad",
            "agents": [{"name": "a", "role": "architect"}],
            "task_graph": [
                {
                    "id": "ask",
                    "assigned_to": "a",
                    "description": "x",
                    "stages": [
                        {"name": "s", "kind": "weird"}
                    ],
                }
            ],
        },
    )
    with pytest.raises(AgoraError, match="unknown kind"):
        load_flow(tmp_path / "bad.yaml")


# ---------------------------------------------------------------------- runtime tests


@pytest.fixture
def _identity() -> AgentIdentity:
    return AgentIdentity(
        agent_id="architect",
        room_id="!agent:x",
        config=AgentConfig(name="architect", role=AgentRole.ARCHITECT),
    )


async def test_decision_stage_resolves_via_reaction(tmp_path: Path, _identity):
    """User reacts with 1️⃣; control maps emoji→answer; stage writes 'approve' to disk."""
    runtime, control, client = _make_runtime(tmp_path)
    runner = StageRunner(runtime)
    staged = _decision_staged_task(
        "brief_approval", ["approve", "revise"], "plan/brief_approval.txt"
    )

    async def _simulate_reaction():
        await asyncio.sleep(0.02)
        # First event sent is the question card; capture its id.
        card_event_id = "$evt_1"  # fake client numbers events sequentially
        # Build a fake reaction event and dispatch through control.handle_reaction.
        from types import SimpleNamespace

        reaction = SimpleNamespace(
            target_event_id=card_event_id,
            sender="@fabs:agora.local",
            key=_EMOJI_DIGITS[0],  # "1️⃣" → first option ("approve")
        )
        await control.handle_reaction("!room:x", reaction)

    results = await asyncio.gather(
        runner.execute_staged_task(staged, _identity),
        _simulate_reaction(),
    )
    outcome = results[0]
    assert outcome.success is True
    # Answer file written.
    assert (tmp_path / "plan/brief_approval.txt").read_text(encoding="utf-8") == "approve"
    # Two Matrix sends: question card + poll event.
    assert len(client.sent) == 2
    assert client.sent[0][1] == "m.room.message"
    assert client.sent[1][1] == "m.poll.start"
    # Question card body lists the decision id and options.
    card_body = client.sent[0][2]["body"]
    assert "brief_approval" in card_body
    assert "approve" in card_body
    assert "revise" in card_body


async def test_decision_stage_resolves_via_chat_command(tmp_path: Path, _identity):
    """User types `/agora decision revise`; control resolves + stage writes to disk."""
    from agora.observe.commands import parse_command

    runtime, control, _client = _make_runtime(tmp_path)
    runner = StageRunner(runtime)
    staged = _decision_staged_task(
        "confirm", ["approve", "revise"], "plan/confirm.txt"
    )

    async def _simulate_command():
        await asyncio.sleep(0.02)
        cmd = parse_command("/agora decision revise", sender="@fabs:x")
        await control.handle_command("!room:x", cmd)

    results = await asyncio.gather(
        runner.execute_staged_task(staged, _identity),
        _simulate_command(),
    )
    assert results[0].success is True
    assert (tmp_path / "plan/confirm.txt").read_text(encoding="utf-8") == "revise"


async def test_decision_stage_resolves_via_poll_response(tmp_path: Path, _identity):
    """User clicks a poll option; control.resolve_decision is called directly."""
    runtime, control, _client = _make_runtime(tmp_path)
    runner = StageRunner(runtime)
    staged = _decision_staged_task(
        "library", ["stdlib-only", "click"], "plan/decision_library.txt"
    )

    async def _simulate_poll_click():
        await asyncio.sleep(0.02)
        # Second event sent is the poll (id $evt_2). Look it up via the control's
        # poll_event_to_decision map — that's exactly what the dispatcher does.
        decision_id = control.decision_id_for_poll("$evt_2")
        assert decision_id == "library"
        control.resolve_decision(decision_id, "click")

    results = await asyncio.gather(
        runner.execute_staged_task(staged, _identity),
        _simulate_poll_click(),
    )
    assert results[0].success is True
    assert (tmp_path / "plan/decision_library.txt").read_text(encoding="utf-8") == "click"


async def test_decision_stage_unknown_emoji_ignored(tmp_path: Path, _identity):
    """Reacting with an unmapped emoji doesn't resolve the decision."""
    from types import SimpleNamespace

    runtime, control, _client = _make_runtime(tmp_path)
    runner = StageRunner(runtime)
    staged = _decision_staged_task(
        "library", ["a", "b"], "plan/decision_library.txt"
    )

    async def _bad_then_good():
        await asyncio.sleep(0.02)
        # Unmapped emoji — should be ignored.
        await control.handle_reaction(
            "!room:x",
            SimpleNamespace(target_event_id="$evt_1", sender="@u:x", key="👍"),
        )
        await asyncio.sleep(0.02)
        # Correct emoji — resolves.
        await control.handle_reaction(
            "!room:x",
            SimpleNamespace(target_event_id="$evt_1", sender="@u:x", key=_EMOJI_DIGITS[1]),
        )

    results = await asyncio.gather(
        runner.execute_staged_task(staged, _identity),
        _bad_then_good(),
    )
    assert results[0].success is True
    assert (tmp_path / "plan/decision_library.txt").read_text(encoding="utf-8") == "b"


async def test_decision_stage_writes_answer_under_nested_path(tmp_path: Path, _identity):
    """Output directory is auto-created."""
    runtime, control, _client = _make_runtime(tmp_path)
    runner = StageRunner(runtime)
    staged = _decision_staged_task("d", ["a", "b"], "deep/nested/path/ans.txt")

    async def _resolve():
        await asyncio.sleep(0.02)
        control.resolve_decision("d", "a")

    await asyncio.gather(runner.execute_staged_task(staged, _identity), _resolve())
    assert (tmp_path / "deep/nested/path/ans.txt").read_text(encoding="utf-8") == "a"


async def test_decision_stage_times_out(tmp_path: Path, _identity, monkeypatch):
    """If no vote arrives, the stage fails with a timeout error."""
    monkeypatch.setenv("AGORA_DECISION_TIMEOUT_SECONDS", "0.2")
    runtime, _control, _client = _make_runtime(tmp_path)
    runner = StageRunner(runtime)
    staged = _decision_staged_task("never", ["a", "b"], "plan/timeout.txt")
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    # The failure is recorded as a stage-level postcondition failure.
    assert any("timed out" in r[2] for r in outcome.postcondition_results)


async def test_decision_stage_rejects_too_many_options(tmp_path: Path, _identity):
    """Decision stages cap at 9 options (one emoji per digit)."""
    runtime, _control, _client = _make_runtime(tmp_path)
    runner = StageRunner(runtime)
    staged = _decision_staged_task(
        "many",
        [f"opt_{i}" for i in range(10)],
        "plan/many.txt",
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    assert any("max" in r[2] and "supported" in r[2] for r in outcome.postcondition_results)


def test_decision_reaction_map_registration():
    """``register_decision_reactions`` populates the map; ``handle_reaction`` reads it."""
    from agora.fleet.control import OrchestratorControl

    client = _FakeMatrixClient()
    control = OrchestratorControl(
        project_room_id="!room:x", matrix_client=client  # type: ignore[arg-type]
    )
    control.register_decision_reactions(
        "d1", "$msg_1", {"1️⃣": "yes", "2️⃣": "no"}
    )
    assert "$msg_1" in control.decision_reaction_map
    decision_id, emoji_map = control.decision_reaction_map["$msg_1"]
    assert decision_id == "d1"
    assert emoji_map["1️⃣"] == "yes"


def test_decision_reaction_map_rejects_empty_args():
    """Empty decision_id or message_event_id is silently ignored (no map mutation)."""
    from agora.fleet.control import OrchestratorControl

    client = _FakeMatrixClient()
    control = OrchestratorControl(
        project_room_id="!room:x", matrix_client=client  # type: ignore[arg-type]
    )
    control.register_decision_reactions("", "$msg_1", {"x": "y"})
    assert control.decision_reaction_map == {}
    control.register_decision_reactions("d", "", {"x": "y"})
    assert control.decision_reaction_map == {}


# ================================================================ framework_finalize_plan


async def test_framework_finalize_plan_stage_emits_yaml(tmp_path: Path, _identity):
    """``kind: framework_finalize_plan`` serializes a pre-populated plan_draft
    to YAML without any LLM involvement, and round-trips through load_plan."""
    from agora.fleet.stage_runner import Stage
    from agora.plan.builder import PlanDraft

    runtime, _control, _client = _make_runtime(tmp_path)

    # Pre-populate the draft — simulates prior author_* stages having run.
    draft = PlanDraft()
    draft.set_metadata("auto-finalized", "")
    draft.set_agents([{"name": "a", "role": "architect"}])
    draft.add_task("t1", "do it", "a", output_path="out.txt")
    draft.attach_postcondition("t1", "mark_complete", {})
    runtime._ctx.plan_draft = draft

    task = Task(
        id="finalize",
        spec=Specification(
            postconditions=(
                make_predicate("_trivial", "pass", lambda _c: (True, "")),
            ),
            description="finalize",
        ),
        description="finalize",
        agent_id="architect",
        status=TaskStatus.PENDING,
    )
    stage = Stage(
        name="finalize",
        kind="framework_finalize_plan",
        output_path="plan/out.plan.yaml",
    )
    runner = StageRunner(runtime)
    outcome = await runner.execute_staged_task(StagedTask(task=task, stages=[stage]), _identity)
    assert outcome.success is True, outcome.postcondition_results
    emitted = tmp_path / "plan/out.plan.yaml"
    assert emitted.is_file()
    # Round-trip: load the emitted plan.
    from agora.plan.loader import instantiate_plan, load_plan

    reloaded = load_plan(emitted)
    agents, tasks, _ = instantiate_plan(reloaded, project_name="rt")
    assert len(agents) == 1
    assert [t.id for t in tasks] == ["t1"]


async def test_plan_reset_tasks_clears_draft_tasks(tmp_path: Path, _identity):
    """``kind: plan_reset_tasks`` wipes ``plan_draft.tasks`` so a retrying
    compound author task doesn't accumulate slot-N entries from prior
    attempts. Agents and metadata are preserved."""
    from agora.fleet.stage_runner import Stage
    from agora.plan.builder import PlanDraft

    runtime, _control, _client = _make_runtime(tmp_path)
    draft = PlanDraft()
    draft.set_metadata("rt", "")
    draft.set_agents([{"name": "a", "role": "architect"}])
    draft.add_task("stale", "leftover", "a")
    draft.attach_postcondition("stale", "mark_complete", {})
    runtime._ctx.plan_draft = draft
    assert len(draft.tasks) == 1

    task = Task(
        id="author",
        spec=Specification(
            postconditions=(
                make_predicate("_trivial", "pass", lambda _c: (True, "")),
            ),
            description="author",
        ),
        description="author",
        agent_id="architect",
        status=TaskStatus.PENDING,
    )
    stage = Stage(name="reset", kind="plan_reset_tasks")
    runner = StageRunner(runtime)
    outcome = await runner.execute_staged_task(
        StagedTask(task=task, stages=[stage]), _identity
    )
    assert outcome.success is True, outcome.postcondition_results
    assert len(draft.tasks) == 0
    # Agents + metadata preserved.
    assert draft.name == "rt"
    assert [a["name"] for a in draft.agents] == ["a"]


async def test_framework_finalize_plan_fails_on_empty_draft(tmp_path: Path, _identity):
    """Without a populated plan_draft, the stage returns a structured error."""
    from agora.fleet.stage_runner import Stage

    runtime, _control, _client = _make_runtime(tmp_path)
    # No plan_draft set.
    task = Task(
        id="finalize",
        spec=Specification(
            postconditions=(
                make_predicate("_trivial", "pass", lambda _c: (True, "")),
            ),
            description="finalize",
        ),
        description="finalize",
        agent_id="architect",
        status=TaskStatus.PENDING,
    )
    stage = Stage(
        name="finalize",
        kind="framework_finalize_plan",
        output_path="plan/out.plan.yaml",
    )
    runner = StageRunner(runtime)
    outcome = await runner.execute_staged_task(StagedTask(task=task, stages=[stage]), _identity)
    assert outcome.success is False
    assert any(
        "plan_draft is empty" in r[2] or "draft not ready" in r[2]
        for r in outcome.postcondition_results
    )
