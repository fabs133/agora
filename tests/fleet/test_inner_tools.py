from pathlib import Path

import pytest

from agora.core.errors import AgoraError
from agora.core.types import AgentRole
from agora.fleet.inner_tools import (
    ROLE_TOOL_SETS,
    ToolContext,
    get_tool_definitions,
    get_tool_executor,
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
    )


def test_tool_definitions_per_role() -> None:
    arch_tools = {t["name"] for t in get_tool_definitions(AgentRole.ARCHITECT)}
    impl_tools = {t["name"] for t in get_tool_definitions(AgentRole.IMPLEMENTER)}

    # Architect lacks git tools.
    assert "git_commit" not in arch_tools
    # Implementer has git.
    assert "git_commit" in impl_tools
    # Everyone has coordination + filesystem + research.
    assert "mark_complete" in arch_tools
    assert "read_file" in arch_tools
    assert "web_search" in arch_tools


def test_role_tool_sets_coverage() -> None:
    # Every role in the enum should have a tool set defined.
    for role in AgentRole:
        assert role in ROLE_TOOL_SETS


async def test_write_and_read_file(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["write_file"]({"path": "sub/hello.txt", "content": "hi"})
    assert "wrote" in out
    assert (Path(ctx.work_dir) / "sub" / "hello.txt").read_text() == "hi"

    back = await executor["read_file"]({"path": "sub/hello.txt"})
    assert back == "hi"


async def test_byte_io_newline_round_trip_untranslated(ctx: ToolContext) -> None:
    """\\n must survive write_file → read_file → equality untranslated, incl. on
    Windows (no text-mode \\n→CRLF). The equality predicate reads raw bytes, so
    the whole byte-exactness path must agree (determinism-probe §5)."""
    from agora.plan.probe_predicates import postcond_file_content_equals_seed

    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    content = "apple\napricot\navocado\n"
    await executor["write_file"]({"path": "out/x.txt", "content": content})
    # On-disk bytes are exactly the content — no CRLF injected.
    assert (Path(ctx.work_dir) / "out" / "x.txt").read_bytes() == content.encode("utf-8")
    # read_file returns the same \n bytes (no universal-newline translation).
    assert await executor["read_file"]({"path": "out/x.txt"}) == content
    # Equality predicate (raw-byte compare) sees them equal to a seed of the
    # same bytes — the round trip is byte-exact end to end.
    (Path(ctx.work_dir) / "seed.txt").write_bytes(content.encode("utf-8"))
    pred = postcond_file_content_equals_seed(path="out/x.txt", seed_path="seed.txt")
    passed, _reason = pred.evaluate({"work_dir": ctx.work_dir})
    assert passed is True


async def test_write_file_sets_blocked_flag_on_guard_failure(ctx: ToolContext) -> None:
    """A blocked write flips ``ctx.write_file_blocked`` so the next turn's
    tool manifest can hide write_file entirely. The 7B otherwise cycles
    through write_file → ERROR → retry indefinitely."""
    target = Path(ctx.work_dir) / "README.md"
    target.write_text("# preexisting", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    assert ctx.write_file_blocked is False
    out = await executor["write_file"](
        {"path": "README.md", "content": "rewritten"}
    )
    assert out.startswith("ERROR:"), out
    assert ctx.write_file_blocked is True


async def test_write_file_refuses_overwrite_of_same_task_content(ctx: ToolContext) -> None:
    """Once a file has real content, write_file refuses — even within the
    same task that created it. Forces the model into edit_file_replace for
    incremental changes, preventing self-clobbering with truncated rewrites."""
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    first = await executor["write_file"](
        {"path": "src/cli.py", "content": "import argparse\n\ndef main(): pass\n"}
    )
    assert "wrote" in first, first
    # Second write targets a file that now has content → refused.
    out = await executor["write_file"](
        {"path": "src/cli.py", "content": "import argparse\n"}
    )
    assert out.startswith("ERROR:"), out
    assert "already exists" in out
    assert "edit_file_replace" in out
    # Original content preserved.
    assert "def main" in (Path(ctx.work_dir) / "src/cli.py").read_text()


async def test_write_file_allows_overwrite_of_empty_placeholder(ctx: ToolContext) -> None:
    """Empty files (size 0) are writeable — scaffolding patterns often
    touch a placeholder first and fill it later in the same task."""
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    await executor["write_file"]({"path": "src/cli.py", "content": ""})
    out = await executor["write_file"](
        {"path": "src/cli.py", "content": "import argparse\n"}
    )
    assert "wrote" in out, out
    assert (Path(ctx.work_dir) / "src/cli.py").read_text() == "import argparse\n"


async def test_write_file_refuses_overwrite_of_preexisting(ctx: ToolContext) -> None:
    """A file with content from any source (prior task, manual scaffolding)
    is protected."""
    target = Path(ctx.work_dir) / "src/cli.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# preexisting content\n", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["write_file"](
        {"path": "src/cli.py", "content": "import argparse\n"}
    )
    assert out.startswith("ERROR:"), out
    assert "already exists" in out
    assert target.read_text() == "# preexisting content\n"


async def test_write_file_force_overrides_guard(ctx: ToolContext) -> None:
    """``force=true`` bypasses the guard for genuine full-rewrites."""
    target = Path(ctx.work_dir) / "src/cli.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["write_file"](
        {"path": "src/cli.py", "content": "new", "force": True}
    )
    assert "wrote" in out, out
    assert target.read_text() == "new"


async def test_read_file_missing(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["read_file"]({"path": "no.txt"})
    assert out.startswith("ERROR")


async def test_list_directory(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    await executor["write_file"]({"path": "a.txt", "content": "a"})
    await executor["write_file"]({"path": "b.txt", "content": "b"})
    listing = await executor["list_directory"]({"path": "."})
    assert "a.txt" in listing and "b.txt" in listing


async def test_delete_file_removes_a_file(ctx: ToolContext) -> None:
    target = Path(ctx.work_dir) / "junk.txt"
    target.write_text("bye", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    ctx.written_files.append("junk.txt")
    out = await executor["delete_file"]({"path": "junk.txt"})
    assert out == "deleted junk.txt"
    assert not target.exists()
    assert "junk.txt" not in ctx.written_files


async def test_delete_file_removes_empty_directory(ctx: ToolContext) -> None:
    target = Path(ctx.work_dir) / "empty_dir"
    target.mkdir()
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["delete_file"]({"path": "empty_dir"})
    assert out == "deleted empty_dir"
    assert not target.exists()


async def test_delete_file_refuses_nonempty_directory(ctx: ToolContext) -> None:
    target = Path(ctx.work_dir) / "d"
    target.mkdir()
    (target / "file.txt").write_text("x", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["delete_file"]({"path": "d"})
    assert out.startswith("ERROR:")
    assert "non-empty" in out
    assert target.is_dir()


async def test_delete_file_missing_path(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["delete_file"]({"path": "ghost.txt"})
    assert out.startswith("ERROR:")
    assert "does not exist" in out


async def test_write_file_refuses_module_vs_package_collision(ctx: ToolContext) -> None:
    """Writing src/foo.py when src/foo/ exists surfaces the import trap
    before it runs — Python would resolve imports to the package."""
    pkg = Path(ctx.work_dir) / "src" / "foo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["write_file"](
        {"path": "src/foo.py", "content": "class Bar: pass\n"}
    )
    assert out.startswith("ERROR:"), out
    assert "collide" in out
    assert "delete_file" in out
    assert not (Path(ctx.work_dir) / "src" / "foo.py").exists()


async def test_write_file_refuses_package_vs_module_collision(ctx: ToolContext) -> None:
    """Writing src/foo/__init__.py when src/foo.py exists — same trap,
    opposite direction."""
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "foo.py").write_text(
        "class Bar: pass\n", encoding="utf-8"
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["write_file"](
        {"path": "src/foo/__init__.py", "content": ""}
    )
    assert out.startswith("ERROR:"), out
    assert "collide" in out


async def test_write_file_force_overrides_collision_check(ctx: ToolContext) -> None:
    """force=true lets the model override the collision guard when the
    clash is genuinely intentional."""
    pkg = Path(ctx.work_dir) / "src" / "foo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["write_file"](
        {"path": "src/foo.py", "content": "class Bar: pass\n", "force": True}
    )
    assert "wrote" in out, out
    assert (Path(ctx.work_dir) / "src" / "foo.py").is_file()


async def test_path_traversal_is_blocked(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="escapes work_dir"):
        await executor["write_file"]({"path": "../escape.txt", "content": "nope"})


async def test_request_review_is_rate_limited_per_task(ctx: ToolContext) -> None:
    """request_review only posts one Matrix event per task. Subsequent calls
    no-op with a hint — stops 7B from spam-reviewing on every turn."""
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    first = await executor["request_review"]({"summary": "done"})
    assert first == "review requested"
    assert len(ctx.reviews_requested) == 1
    second = await executor["request_review"]({"summary": "done again"})
    assert "already requested" in second
    assert len(ctx.reviews_requested) == 1  # not appended a second time


async def test_report_progress_sends_matrix_event(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["report_progress"]({"message": "half done"})
    assert "reported" in out
    room = ctx.matrix_client.rooms[ctx.project_room_id] if ctx.project_room_id in ctx.matrix_client.rooms else None
    # Project room not auto-created by ToolContext; we just assert the call was logged.
    assert ctx.progress_log == [{"message": "half done"}]


async def test_mark_complete_records_artifacts(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    await executor["mark_complete"]({"summary": "done", "artifacts": ["out.py"]})
    assert ctx.completions == [{"summary": "done", "artifacts": ["out.py"]}]


async def test_report_learning_records(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    await executor["report_learning"](
        {"category": "pattern", "content": "prefer DI", "confidence": 0.8}
    )
    assert ctx.reported_learnings == [
        {"category": "pattern", "content": "prefer DI", "confidence": 0.8}
    ]


async def test_search_disabled_returns_error(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    out = await executor["web_search"]({"query": "anything"})
    assert "not enabled" in out


async def test_git_without_repo_returns_error(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await executor["git_commit"]({"message": "nope"})
    assert "no git repo" in out
