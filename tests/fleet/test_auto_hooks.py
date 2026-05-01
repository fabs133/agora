"""Unit tests for automatic tool hooks that fire after ``write_file``."""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.core.types import AgentRole
from agora.fleet.auto_hooks import (
    AutoHookResult,
    run_auto_hooks,
    synthesize_mark_complete,
)
from agora.fleet.inner_tools import (
    AUTO_HOOKED_TOOL_NAMES,
    ToolContext,
    get_tool_definitions,
)


@pytest.fixture
async def ctx(tmp_path: Path, fake_matrix_client) -> ToolContext:
    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")
    return ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        auto_hooks_enabled=True,
    )


# ---------------------------------------------------------- get_tool_definitions


def test_get_tool_definitions_hides_auto_hooked_tools_when_enabled() -> None:
    names = {t["name"] for t in get_tool_definitions(AgentRole.IMPLEMENTER)}
    assert "check_python" in names
    assert "git_commit" in names
    assert "mark_complete" in names

    names_gated = {
        t["name"]
        for t in get_tool_definitions(AgentRole.IMPLEMENTER, auto_hooks_enabled=True)
    }
    assert "check_python" not in names_gated
    assert "git_commit" not in names_gated
    # v2.7: mark_complete is NOT hidden anymore — weak models otherwise
    # reach for post_note with wrong arg shapes when they want to signal
    # completion. Auto-hook still synthesises when unused.
    assert "mark_complete" in names_gated
    # Core tools still exposed.
    assert "write_file" in names_gated
    assert "read_file" in names_gated


def test_auto_hooked_tool_names_is_stable() -> None:
    # Regression: the set defines what the agent does NOT see. Widening it
    # silently hides tools from the LLM.
    assert AUTO_HOOKED_TOOL_NAMES == frozenset(
        {"check_python", "git_commit", "git_diff", "git_log", "report_learning"}
    )


# ---------------------------------------------------------------- run_auto_hooks


async def test_run_auto_hooks_disabled_is_noop(tmp_path, fake_matrix_client) -> None:
    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=agent_room,
        auto_hooks_enabled=False,
    )
    hooks = await run_auto_hooks(
        "write_file", {"path": "x.py"}, "wrote 10 bytes to x.py", ctx
    )
    assert hooks == []


async def test_run_auto_hooks_ignores_non_write_tools(ctx: ToolContext) -> None:
    hooks = await run_auto_hooks(
        "read_file", {"path": "x.py"}, "file contents", ctx
    )
    assert hooks == []


async def test_run_auto_hooks_ignores_failed_write(ctx: ToolContext) -> None:
    hooks = await run_auto_hooks(
        "write_file", {"path": "x.py"}, "ERROR: something", ctx
    )
    assert hooks == []


async def test_run_auto_hooks_py_file_runs_check_python_then_import(
    ctx: ToolContext,
) -> None:
    (Path(ctx.work_dir) / "good.py").write_text(
        "import math\nX = math.pi\n", encoding="utf-8"
    )
    hooks = await run_auto_hooks(
        "write_file", {"path": "good.py"}, "wrote 30 bytes to good.py", ctx
    )
    tool_names = [h.tool_name for h in hooks]
    assert "check_python" in tool_names
    assert "run_python_import" in tool_names
    # All should have succeeded.
    assert all(h.success for h in hooks if h.tool_name != "git_commit")


async def test_run_auto_hooks_stops_on_check_python_failure(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "bad.py").write_text(
        "def broken(\n", encoding="utf-8"  # syntax error
    )
    hooks = await run_auto_hooks(
        "write_file", {"path": "bad.py"}, "wrote 12 bytes to bad.py", ctx
    )
    names = [h.tool_name for h in hooks]
    assert names == ["check_python"]
    assert not hooks[0].success


async def test_run_auto_hooks_requirements_triggers_check_requirements(
    ctx: ToolContext,
) -> None:
    (Path(ctx.work_dir) / "requirements.txt").write_text(
        "discord.py>=2.3\n", encoding="utf-8"
    )
    hooks = await run_auto_hooks(
        "write_file",
        {"path": "requirements.txt"},
        "wrote 17 bytes to requirements.txt",
        ctx,
    )
    names = [h.tool_name for h in hooks]
    assert "check_requirements" in names


async def test_run_auto_hooks_requirements_flags_import_statement(
    ctx: ToolContext,
) -> None:
    (Path(ctx.work_dir) / "requirements.txt").write_text(
        "import discord\ndiscord.py>=2.3\n", encoding="utf-8"
    )
    hooks = await run_auto_hooks(
        "write_file",
        {"path": "requirements.txt"},
        "wrote 31 bytes to requirements.txt",
        ctx,
    )
    cr_hook = next(h for h in hooks if h.tool_name == "check_requirements")
    assert not cr_hook.success
    # git_commit must NOT fire when validation fails.
    assert not any(h.tool_name == "git_commit" for h in hooks)


async def test_run_auto_hooks_skips_git_commit_when_no_repo(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "good.py").write_text(
        "import math\n", encoding="utf-8"
    )
    ctx.git_repo = None
    hooks = await run_auto_hooks(
        "write_file", {"path": "good.py"}, "wrote 12 bytes to good.py", ctx
    )
    assert not any(h.tool_name == "git_commit" for h in hooks)


# ---------------------------------------------------------- synthesize_mark_complete


def test_synthesize_mark_complete_appends_entry_when_empty() -> None:
    ctx = ToolContext(
        work_dir="/tmp", matrix_client=None, agent_room_id="", project_room_id="",  # type: ignore[arg-type]
    )
    ctx.written_files = ["bot.py", "README.md"]
    appended = synthesize_mark_complete(ctx, "All done.")
    assert appended is True
    assert len(ctx.completions) == 1
    entry = ctx.completions[0]
    assert entry["summary"] == "All done."
    assert entry["artifacts"] == ["bot.py", "README.md"]
    assert entry.get("auto") is True


def test_synthesize_mark_complete_is_noop_when_agent_already_completed() -> None:
    ctx = ToolContext(
        work_dir="/tmp", matrix_client=None, agent_room_id="", project_room_id="",  # type: ignore[arg-type]
    )
    ctx.completions = [{"summary": "agent said done", "artifacts": []}]
    appended = synthesize_mark_complete(ctx, "late text")
    assert appended is False
    assert ctx.completions == [{"summary": "agent said done", "artifacts": []}]


def test_synthesize_mark_complete_handles_empty_final_text() -> None:
    ctx = ToolContext(
        work_dir="/tmp", matrix_client=None, agent_room_id="", project_room_id="",  # type: ignore[arg-type]
    )
    synthesize_mark_complete(ctx, "")
    assert ctx.completions[0]["summary"].startswith("(auto-synthesized")


def test_synthesize_mark_complete_truncates_long_summary() -> None:
    ctx = ToolContext(
        work_dir="/tmp", matrix_client=None, agent_room_id="", project_room_id="",  # type: ignore[arg-type]
    )
    long_first_line = "x" * 500 + "\nsecond"
    synthesize_mark_complete(ctx, long_first_line)
    assert len(ctx.completions[0]["summary"]) <= 200
