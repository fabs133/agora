"""End-to-end orchestrator tests with the observer enabled.

These exercise the `run_project` path with `enable_observer=True`: SyncService,
Renderer, /agora command routing, and the pause/abort gate in `_run_phase`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agora.core.agent import AgentConfig
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole, ProjectPhase, TaskStatus
from agora.fleet.llm_adapter import LLMResponse
from agora.fleet.orchestrator import Orchestrator
from agora.matrix.room_manager import RoomManager
from tests.conftest import FakeLLM, tool_call


def _always_pass() -> Specification:
    return Specification(
        postconditions=(make_predicate("ok", "", lambda _c: (True, "")),),
        description="trivial",
    )


def _llm_plan_factory():
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


def _orchestrator(
    tmp_path: Path, fake_matrix_client, *, enable_observer: bool = True, repo_root: str | None = None
) -> Orchestrator:
    return Orchestrator(
        matrix_client=fake_matrix_client,
        room_manager=RoomManager(fake_matrix_client, homeserver_name="agora.local"),
        llm_factory=lambda _m: _llm_plan_factory(),
        work_dir=str(tmp_path / "work"),
        max_parallel_agents=1,
        enable_observer=enable_observer,
        skip_warmup=True,  # no real Ollama in tests
        repo_root=repo_root,
        review_timeout_seconds=0.5,  # unit tests don't simulate poll votes
    )


# ------------------------------------------------------------------- rendering


async def test_observer_posts_phase_banners(tmp_path, fake_matrix_client) -> None:
    orch = _orchestrator(tmp_path, fake_matrix_client, enable_observer=True)
    result = await orch.run_project(
        "observed",
        [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)],
        [Task(id="t1", spec=_always_pass(), description="go", agent_id="w")],
    )
    assert result.success is True
    project_room = fake_matrix_client.rooms[result.project_room_id]
    msgs = [e for e in project_room.timeline if e.event_type == "m.room.message"]
    assert msgs, "renderer never posted a formatted message"
    bodies = " ".join(m.content.get("body", "") for m in msgs)
    # At minimum we should see a phase banner + the review summary.
    assert "phase:" in bodies.lower()
    assert "review" in bodies.lower()


async def test_observer_disabled_produces_no_room_messages(tmp_path, fake_matrix_client) -> None:
    orch = _orchestrator(tmp_path, fake_matrix_client, enable_observer=False)
    result = await orch.run_project(
        "quiet",
        [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)],
        [Task(id="t1", spec=_always_pass(), description="go", agent_id="w")],
    )
    assert result.success is True
    project_room = fake_matrix_client.rooms[result.project_room_id]
    msgs = [e for e in project_room.timeline if e.event_type == "m.room.message"]
    assert msgs == []


# ------------------------------------------------------------------- pause + abort


async def test_pause_blocks_task_dispatch_until_resume(tmp_path, fake_matrix_client) -> None:
    """Paused project shouldn't dispatch new tasks; resume releases the gate."""

    class _SlowLLM:
        def __init__(self) -> None:
            self._turns = 0

        async def complete(self, **_k):
            self._turns += 1
            await asyncio.sleep(0.25)
            if self._turns % 3 == 1:
                return LLMResponse(
                    content="",
                    tool_calls=(tool_call("mark_complete", {"summary": "d"}),),
                )
            if self._turns % 3 == 2:
                return LLMResponse(content="done")
            return LLMResponse(content="[]")

        def format_assistant_turn(self, resp):
            from agora.fleet.llm_adapter import _AnthropicShape

            return _AnthropicShape().format_assistant_turn(resp)

        def format_tool_results(self, calls, results):
            from agora.fleet.llm_adapter import _AnthropicShape

            return _AnthropicShape().format_tool_results(calls, results)

    orch = Orchestrator(
        matrix_client=fake_matrix_client,
        room_manager=RoomManager(fake_matrix_client, homeserver_name="agora.local"),
        llm_factory=lambda _m: _SlowLLM(),
        work_dir=str(tmp_path),
        max_parallel_agents=1,
        enable_observer=True,
        skip_warmup=True,
        review_timeout_seconds=0.5,
    )
    agents = [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)]
    tasks = [
        Task(id=f"t{i}", spec=_always_pass(), description=f"t{i}", agent_id="w")
        for i in range(3)
    ]
    run = asyncio.create_task(orch.run_project("pausey", agents, tasks))
    # Give the orchestrator time to create rooms and register the control.
    await asyncio.sleep(0.15)

    project_rooms = [
        r for r in fake_matrix_client.rooms.values() if r.name.startswith("project:")
    ]
    assert project_rooms, "project room never created"
    project_room_id = project_rooms[0].room_id
    control = orch.get_control(project_room_id)
    assert control is not None, "control not registered during run"

    # Pause, then resume after a beat — the run should still complete successfully.
    control.pause_event.clear()
    await asyncio.sleep(0.2)
    control.pause_event.set()

    result = await asyncio.wait_for(run, timeout=15)
    assert result.success is True


async def test_abort_transitions_project_to_failed(tmp_path, fake_matrix_client) -> None:
    class _StallingLLM:
        """mark_complete then stop, but each task pauses briefly so abort can land."""

        def __init__(self) -> None:
            self._turns = 0

        async def complete(self, **_k):
            self._turns += 1
            await asyncio.sleep(0.2)
            if self._turns == 1:
                return LLMResponse(
                    content="", tool_calls=(tool_call("mark_complete", {"summary": "d"}),)
                )
            if self._turns == 2:
                return LLMResponse(content="done")
            # reflection pass
            return LLMResponse(content="[]")

        def format_assistant_turn(self, resp):
            from agora.fleet.llm_adapter import _AnthropicShape

            return _AnthropicShape().format_assistant_turn(resp)

        def format_tool_results(self, calls, results):
            from agora.fleet.llm_adapter import _AnthropicShape

            return _AnthropicShape().format_tool_results(calls, results)

    orch = Orchestrator(
        matrix_client=fake_matrix_client,
        room_manager=RoomManager(fake_matrix_client, homeserver_name="agora.local"),
        llm_factory=lambda _m: _StallingLLM(),
        work_dir=str(tmp_path),
        max_parallel_agents=1,
        enable_observer=True,
        skip_warmup=True,
        review_timeout_seconds=0.5,
    )
    agents = [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)]
    tasks = [
        Task(id=f"t{i}", spec=_always_pass(), description=f"t{i}", agent_id="w")
        for i in range(5)
    ]
    run = asyncio.create_task(orch.run_project("abortme", agents, tasks))
    await asyncio.sleep(0.05)

    project_rooms = [
        r for r in fake_matrix_client.rooms.values() if r.name.startswith("project:")
    ]
    project_room_id = project_rooms[0].room_id
    control = orch.get_control(project_room_id)
    assert control is not None
    control.abort_reason = "observer bailed"
    control.abort_event.set()
    control.pause_event.set()  # wake any waiter

    result = await asyncio.wait_for(run, timeout=10)
    assert result.success is False
    assert result.project.phase == ProjectPhase.FAILED
    # The abort reason is captured in the FAILED transition.
    failed_change = next(
        c for c in result.project.phase_history if c.to_phase == ProjectPhase.FAILED
    )
    assert "observer bailed" in failed_change.reason


# ------------------------------------------------------------------- knowledge upload


async def test_knowledge_files_uploaded_to_identity_room(
    tmp_path, fake_matrix_client
) -> None:
    kb = tmp_path / "kb.md"
    kb.write_text("# notes\nprefer DI\n", encoding="utf-8")

    orch = _orchestrator(tmp_path, fake_matrix_client, enable_observer=False)
    result = await orch.run_project(
        "kb-proj",
        [
            AgentConfig(
                name="w",
                role=AgentRole.IMPLEMENTER,
                knowledge_files=(str(kb),),
            )
        ],
        [Task(id="t1", spec=_always_pass(), description="go", agent_id="w")],
    )
    assert result.success is True

    # The agent's identity room should contain a knowledge_ref state event.
    agent_room = next(
        r for r in fake_matrix_client.rooms.values() if r.name == "agent:w"
    )
    ref_states = [
        ev for ev in agent_room.state.values()
        if ev.event_type == "m.agora.knowledge_ref"
    ]
    assert len(ref_states) == 1
    # The upload is recorded.
    assert len(fake_matrix_client.uploads) == 1
    assert fake_matrix_client.uploads[0][0] == str(kb)


# ------------------------------------------------------------------- repo wiring


async def test_repo_root_creates_per_project_git_repo(tmp_path, fake_matrix_client) -> None:
    """Git repo lives at ``work_dir/<project>`` — same dir agents write into.

    Previously the orchestrator kept files (in ``work_dir``) and the git repo
    (in ``repo_root/<project>``) in different directories, so ``git_commit``
    silently no-op'd. After the unification, both are the same path.
    """
    orch = _orchestrator(
        tmp_path, fake_matrix_client, enable_observer=False, repo_root=None
    )
    result = await orch.run_project(
        "gitproj",
        [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)],
        [Task(id="t1", spec=_always_pass(), description="go", agent_id="w")],
    )
    assert result.success is True
    # Unified: project work_dir and git repo are the same directory.
    project_dir = tmp_path / "work" / "gitproj"
    assert project_dir.is_dir()
    assert (project_dir / "README.md").is_file()
    assert (project_dir / ".git").is_dir()
    assert result.project.git_repo_path == str(project_dir)


async def test_work_dir_and_repo_path_coincide(tmp_path, fake_matrix_client) -> None:
    """A file written via ``write_file`` lands inside the git working tree."""
    from agora.fleet.inner_tools import ToolContext, get_tool_executor

    orch = _orchestrator(tmp_path, fake_matrix_client, enable_observer=False)
    project_work_dir = orch._project_work_dir("demo")
    repo = orch._make_repo_manager("demo")
    assert repo is not None
    assert str(repo.repo_path) == project_work_dir

    ctx = ToolContext(
        work_dir=project_work_dir,
        matrix_client=fake_matrix_client,
        agent_room_id="!r:agora.local",
        project_room_id="!p:agora.local",
        git_repo=repo,
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    await executor["write_file"]({"path": "hello.py", "content": "x = 1\n"})
    assert (Path(project_work_dir) / "hello.py").is_file()
    assert (Path(project_work_dir) / ".git").is_dir()


# ---------------------------------------------------------- auto-learning wiring


async def test_failed_postcondition_records_auto_learning(
    tmp_path, fake_matrix_client
) -> None:
    """A failing postcondition must produce an m.agora.learning event without
    the agent calling report_learning."""
    from agora.core.contract import Specification, make_predicate

    always_fail = Specification(
        postconditions=(
            make_predicate(
                "requires_widget",
                "checks that widget key is present",
                lambda _c: (False, "expected 'widget' key in output"),
            ),
        ),
        description="trivial fail",
    )
    orch = _orchestrator(tmp_path, fake_matrix_client, enable_observer=False)
    config = AgentConfig(name="impl", role=AgentRole.IMPLEMENTER)
    task = Task(id="fail_task", spec=always_fail, description="go", agent_id="impl")

    await orch.run_project("auto_learn", [config], [task])

    # Find the agent identity room — only one non-project room was created.
    room_ids = list(fake_matrix_client.rooms.keys())
    identity_rooms = [r for r in room_ids if r != _last_project_room(fake_matrix_client)]
    assert identity_rooms, "no identity room created"
    agent_room = fake_matrix_client.rooms[identity_rooms[0]]
    learning_events = [
        e for e in agent_room.timeline
        if e.event_type == "m.agora.learning"
           or (isinstance(e.content, dict) and e.content.get("com.agora.type") == "m.agora.learning")
    ]
    assert learning_events, "expected at least one auto-learning posted"
    # Peek at the payload: envelope or native.
    payload = learning_events[0].content
    inner = payload.get("com.agora.data", payload) if isinstance(payload, dict) else {}
    content_blob = inner.get("content") if isinstance(inner, dict) else ""
    assert "[auto]" in (content_blob or "")
    assert "requires_widget" in (content_blob or "")


def _last_project_room(client) -> str:
    """Helper — last room whose name starts with 'project' is the project room."""
    for rid, room in reversed(list(client.rooms.items())):
        if getattr(room, "name", "").startswith("project"):
            return rid
    return ""


async def test_auto_learning_is_injected_for_retry(
    tmp_path, fake_matrix_client
) -> None:
    """After a postcondition failure, the agent's in-memory learned_patterns
    includes the synthesized learning — so a same-run retry sees it."""
    from agora.core.agent import AgentIdentity
    from agora.core.contract import Specification, make_predicate
    from agora.core.task import Task
    from agora.fleet.agent_runtime import TaskResult

    orch = _orchestrator(tmp_path, fake_matrix_client, enable_observer=False)
    identity = AgentIdentity(
        agent_id="impl",
        room_id=await fake_matrix_client.create_room(name="agent", topic=""),
        config=AgentConfig(name="impl", role=AgentRole.IMPLEMENTER),
    )
    task = Task(
        id="t",
        spec=Specification(
            postconditions=(
                make_predicate("p", "desc", lambda _c: (False, "boom")),
            ),
            description="",
        ),
        description="go",
        agent_id="impl",
    )
    outcome = TaskResult(
        task_id="t",
        success=False,
        output="",
        postcondition_results=[("p", False, "boom")],
    )

    assert identity.learned_patterns == []
    await orch._record_failure_learnings(identity, task, outcome)
    assert len(identity.learned_patterns) == 1
    learning = identity.learned_patterns[0]
    assert learning.content.startswith("[auto]")
    assert "t" in learning.content
    assert "p" in learning.content
