"""Tests for the three observer-UX gaps closed in Round 9:

1. Review summary shows what's on disk (files + commits + failures).
2. Write-event cards land in the project room after every auto-hook chain.
3. ``/agora comment <task_id> <text>`` attaches per-task reviewer feedback.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole, ProjectPhase
from agora.fleet.agent_runtime import AgentRuntime
from agora.fleet.control import OrchestratorControl
from agora.fleet.inner_tools import ToolContext
from agora.fleet.llm_adapter import LLMResponse
from agora.observe.commands import VERB_COMMENT, parse_command
from agora.observe.formatters import (
    ArtifactSnapshot,
    format_review_summary,
    format_write_event,
)
from agora.observe.review import _gather_artifact_snapshot
from tests.conftest import FakeLLM, tool_call


# ==============================================================================
# Gap 1 — review summary with artifact previews
# ==============================================================================


def test_review_summary_includes_file_tree_when_artifact_supplied() -> None:
    snapshot = ArtifactSnapshot(
        files=[("bot.py", 929), ("requirements.txt", 17), ("README.md", 431)],
        recent_commits=["abc1234 auto: wrote bot.py", "def5678 chore: init"],
        postcondition_failures=[],
    )
    msg = format_review_summary(
        project_name="discord-bot",
        phase=ProjectPhase.REVIEW,
        task_results_summary=[],
        artifact=snapshot,
    )
    assert "bot.py" in msg.body
    assert "929" in msg.body
    assert "abc1234" in msg.body
    assert "Files on disk" in msg.formatted_body
    assert "Recent commits" in msg.formatted_body


def test_review_summary_without_artifact_stays_backwards_compatible() -> None:
    msg = format_review_summary(
        project_name="demo",
        phase=ProjectPhase.REVIEW,
        task_results_summary=[{"success": True, "task_id": "t", "description": "d"}],
    )
    # No artifact pane keywords.
    assert "Files on disk" not in msg.formatted_body
    assert "Recent commits" not in msg.formatted_body


def test_review_summary_lists_failed_postconditions() -> None:
    snapshot = ArtifactSnapshot(
        files=[],
        recent_commits=[],
        postcondition_failures=[
            ("build_roll", "bot_py_imports", "AttributeError: module 'discord' has no attribute 'InterACTION'"),
            ("write_readme", "README.md_has_DISCORD_TOKEN", "README.md does not contain 'DISCORD_TOKEN'"),
        ],
    )
    msg = format_review_summary(
        project_name="x",
        phase=ProjectPhase.REVIEW,
        task_results_summary=[],
        artifact=snapshot,
    )
    assert "build_roll" in msg.body
    assert "bot_py_imports" in msg.body
    assert "AttributeError" in msg.body
    assert "README.md_has_DISCORD_TOKEN" in msg.formatted_body


def test_review_summary_truncates_very_long_file_lists() -> None:
    files = [(f"file{i}.py", 100) for i in range(50)]
    snapshot = ArtifactSnapshot(files=files, recent_commits=[], postcondition_failures=[])
    msg = format_review_summary("p", ProjectPhase.REVIEW, [], artifact=snapshot)
    assert "+30 more" in msg.body  # 50 - 20 shown = 30 hidden


def test_gather_artifact_snapshot_scans_work_dir_files(tmp_path: Path) -> None:
    (tmp_path / "bot.py").write_text("x" * 100, encoding="utf-8")
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "intro.md").write_text("y" * 50, encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cache.pyc").write_bytes(b"skip")

    snapshot = _gather_artifact_snapshot(str(tmp_path), [])
    paths = {p for p, _ in snapshot.files}
    assert "bot.py" in paths
    assert "kb/intro.md" in paths
    assert not any(p.startswith("__pycache__") for p in paths)


def test_gather_artifact_snapshot_pulls_git_log(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=tmp_path, check=True
    )
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial", "--no-gpg-sign"], cwd=tmp_path, check=True
    )

    snapshot = _gather_artifact_snapshot(str(tmp_path), [])
    assert snapshot.recent_commits
    assert any("initial" in c for c in snapshot.recent_commits)


def test_gather_artifact_snapshot_extracts_postcondition_failures() -> None:
    results = [
        {
            "task_id": "build_roll",
            "success": False,
            "postcondition_results": [
                ("bot_py_imports", False, "AttributeError: ..."),
                ("file_exists_bot", True, ""),
            ],
        },
        {"task_id": "build_skeleton", "success": True, "postcondition_results": []},
    ]
    snapshot = _gather_artifact_snapshot(None, results)
    assert snapshot.postcondition_failures == [
        ("build_roll", "bot_py_imports", "AttributeError: ..."),
    ]


def test_gather_artifact_snapshot_without_work_dir_returns_empty_files() -> None:
    snapshot = _gather_artifact_snapshot(None, [])
    assert snapshot.files == []
    assert snapshot.recent_commits == []


# ==============================================================================
# Gap 2 — write-event cards in the project room
# ==============================================================================


def test_format_write_event_renders_path_operation_and_chain() -> None:
    msg = format_write_event(
        task_id="build_ping",
        path="bot.py",
        operation="edit:insert_before",
        size_bytes=929,
        hook_summary=[("check_python", True), ("run_python_import", True), ("git_commit", True)],
    )
    assert "build_ping" in msg.body
    assert "bot.py" in msg.body
    assert "edit:insert_before" in msg.body
    assert "929" in msg.body
    # Chain uses check/cross icons.
    assert "check_python" in msg.body
    assert "run_python_import" in msg.body
    assert "✓" in msg.body


def test_format_write_event_marks_failed_hooks_with_cross() -> None:
    msg = format_write_event(
        task_id="build_roll",
        path="bot.py",
        operation="edit:insert_before",
        size_bytes=800,
        hook_summary=[("check_python", True), ("run_python_import", False)],
    )
    assert "✓" in msg.body
    assert "✗" in msg.body


def test_format_write_event_shows_collapsed_preview_for_small_files() -> None:
    msg = format_write_event(
        task_id="t",
        path="tiny.py",
        operation="write",
        size_bytes=20,
        hook_summary=[],
        preview="x = 1\n",
    )
    assert "<details>" in msg.formatted_body
    assert "x = 1" in msg.formatted_body


async def test_runtime_posts_write_card_after_hook_chain(
    tmp_path: Path, fake_matrix_client
) -> None:
    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")

    class FakeRepo:
        def commit_all(self, message: str) -> str:
            return "deadbeef"

        def stage_changes(self, paths=None) -> list[str]:
            return ["hello.py"]

    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        git_repo=FakeRepo(),
        auto_hooks_enabled=True,
    )
    identity = AgentIdentity(
        agent_id="w",
        room_id=agent_room,
        config=AgentConfig(name="w", role=AgentRole.IMPLEMENTER),
    )
    responses = [
        LLMResponse(
            content="",
            tool_calls=(
                tool_call(
                    "write_file",
                    {"path": "hello.py", "content": "import math\nX = math.pi\n"},
                ),
            ),
        ),
        LLMResponse(content="done"),
        LLMResponse(content="[]"),
    ]
    runtime = AgentRuntime(
        llm=FakeLLM(responses),
        matrix_client=fake_matrix_client,
        tool_context=ctx,
    )
    task = Task(
        id="build_hello",
        spec=Specification(
            postconditions=(make_predicate("ok", "", lambda _c: (True, "")),),
            description="",
        ),
        description="write hello.py",
        agent_id="w",
    )
    await runtime.execute_task(task, identity)

    # The project room should now carry a write-event card mentioning the task.
    project = fake_matrix_client.rooms[project_room]
    write_cards = [
        e
        for e in project.timeline
        if e.event_type == "m.room.message"
        and "build_hello" in e.content.get("body", "")
        and "hello.py" in e.content.get("body", "")
    ]
    assert write_cards, "no write-event card landed in project room"


# ==============================================================================
# Gap 3 — /agora comment <task_id> <text>
# ==============================================================================


def test_parse_command_accepts_comment_verb() -> None:
    cmd = parse_command("/agora comment build_roll the tree.sync is in the wrong place")
    assert cmd is not None
    assert cmd.verb == VERB_COMMENT
    assert cmd.args[0] == "build_roll"
    assert "tree.sync" in " ".join(cmd.args[1:])


def test_parse_command_comment_rejects_missing_text() -> None:
    from agora.observe.commands import validate

    cmd = parse_command("/agora comment build_roll")
    assert cmd is not None
    ok, reason = validate(cmd)
    assert ok is False
    assert "task_id" in reason and "text" in reason


async def test_control_queues_comment_and_acks(fake_matrix_client) -> None:
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)

    cmd = parse_command(
        "/agora comment build_roll use random.randint from the stdlib",
        sender="@fabs:agora.local",
    )
    assert cmd is not None
    await control.handle_command(room, cmd)

    assert "build_roll" in control.task_comments
    assert control.task_comments["build_roll"] == [
        "use random.randint from the stdlib"
    ]
    # An ack message was posted.
    messages = fake_matrix_client.rooms[room].timeline
    assert any("comment queued" in m.content.get("body", "") for m in messages)


async def test_consume_task_comments_is_one_shot() -> None:
    class _M:
        async def send_event(self, *a, **k):
            return "ev"

    control = OrchestratorControl(project_room_id="!r", matrix_client=_M())
    control.task_comments["t"] = ["first", "second"]
    first = control.consume_task_comments("t")
    second = control.consume_task_comments("t")
    assert first == ["first", "second"]
    assert second == []


async def test_system_prompt_injects_task_comments_for_matching_task(
    tmp_path: Path, fake_matrix_client
) -> None:
    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")

    control = OrchestratorControl(
        project_room_id=project_room, matrix_client=fake_matrix_client
    )
    control.task_comments["build_roll"] = [
        "use random.randint from stdlib",
        "/roll should accept sides:int default 6",
    ]
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        control=control,
    )
    identity = AgentIdentity(
        agent_id="w",
        room_id=agent_room,
        config=AgentConfig(name="w", role=AgentRole.IMPLEMENTER, instructions="Base."),
    )
    runtime = AgentRuntime(
        llm=FakeLLM([]), matrix_client=fake_matrix_client, tool_context=ctx
    )
    prompt = runtime._compose_system_prompt(identity, task_id="build_roll")
    assert "Reviewer feedback" in prompt
    assert "random.randint" in prompt
    assert "sides:int" in prompt
    # And a one-shot consume.
    assert control.task_comments.get("build_roll") is None


async def test_system_prompt_ignores_comments_for_other_tasks(
    tmp_path: Path, fake_matrix_client
) -> None:
    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")

    control = OrchestratorControl(
        project_room_id=project_room, matrix_client=fake_matrix_client
    )
    control.task_comments["build_roll"] = ["use random"]
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        control=control,
    )
    identity = AgentIdentity(
        agent_id="w",
        room_id=agent_room,
        config=AgentConfig(name="w", role=AgentRole.IMPLEMENTER),
    )
    runtime = AgentRuntime(
        llm=FakeLLM([]), matrix_client=fake_matrix_client, tool_context=ctx
    )
    prompt = runtime._compose_system_prompt(identity, task_id="build_ping")
    assert "Reviewer feedback" not in prompt
    # Other task's comment still queued — consume wasn't called on it.
    assert control.task_comments.get("build_roll") == ["use random"]
