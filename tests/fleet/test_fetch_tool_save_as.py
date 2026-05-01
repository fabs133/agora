"""Tests for the ``save_as`` parameter on the ``fetch_url`` inner tool.

The motivation: weak models cannot reliably echo 16 KB of fetched text as the
``content=`` argument of a following ``write_file`` call. ``save_as`` lets the
framework write the fetched text to disk in one atomic call, returning a short
summary. This test file pins that behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.core.errors import AgoraError
from agora.core.types import AgentRole
from agora.fleet.inner_tools import ToolContext, get_tool_executor


@pytest.fixture
async def ctx(tmp_path: Path, fake_matrix_client) -> ToolContext:
    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")

    async def _fake_fetcher(url: str) -> str:
        return f"BODY for {url}" * 100  # ~1.7 KB payload — well under any cap

    return ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        fetch_fn=_fake_fetcher,
    )


async def test_fetch_url_without_save_as_returns_content_as_before(
    ctx: ToolContext,
) -> None:
    """Legacy form: no save_as → content comes back as the tool result string."""
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    result = await executor["fetch_url"]({"url": "https://example.com/x"})
    assert result.startswith("BODY for https://example.com/x")
    assert ctx.written_files == []


async def test_fetch_url_save_as_writes_file_with_fetched_content(
    ctx: ToolContext,
) -> None:
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    result = await executor["fetch_url"](
        {"url": "https://example.com/doc", "save_as": "kb/doc.md"}
    )
    written = (Path(ctx.work_dir) / "kb" / "doc.md").read_text(encoding="utf-8")
    assert written.startswith("BODY for https://example.com/doc")
    assert "BODY for" in written
    # Return value is a short summary, not the body.
    assert result.startswith("fetched ")
    assert "kb/doc.md" in result
    assert len(result) < 200


async def test_fetch_url_save_as_populates_written_files(
    ctx: ToolContext,
) -> None:
    """So the auto-hook chain (git_commit) can reach saved fetches."""
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    await executor["fetch_url"](
        {"url": "https://example.com/doc", "save_as": "kb/doc.md"}
    )
    assert "kb/doc.md" in ctx.written_files


async def test_fetch_url_save_as_rejects_path_traversal(
    ctx: ToolContext,
) -> None:
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    with pytest.raises(AgoraError, match="escapes work_dir"):
        await executor["fetch_url"](
            {"url": "https://example.com/doc", "save_as": "../escape.md"}
        )


async def test_fetch_url_save_as_creates_parent_dirs(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    await executor["fetch_url"](
        {"url": "https://example.com/d", "save_as": "deep/nested/path/doc.md"}
    )
    assert (Path(ctx.work_dir) / "deep" / "nested" / "path" / "doc.md").is_file()


async def test_fetch_url_save_as_summary_includes_char_count(
    ctx: ToolContext,
) -> None:
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    result = await executor["fetch_url"](
        {"url": "https://example.com/x", "save_as": "out.md"}
    )
    # "BODY for https://example.com/x" * 100 is 3000 chars.
    assert "fetched 3000 chars" in result or "3000" in result


async def test_fetch_url_schema_advertises_save_as() -> None:
    from agora.fleet.inner_tools import get_tool_definitions

    tools = get_tool_definitions(AgentRole.ARCHITECT)
    fetch = next(t for t in tools if t["name"] == "fetch_url")
    props = fetch["input_schema"]["properties"]
    assert "save_as" in props
    # save_as must remain optional to preserve legacy behaviour.
    assert "save_as" not in fetch["input_schema"].get("required", ["url"])


async def test_fetch_url_auto_hook_commits_saved_file(
    tmp_path: Path, fake_matrix_client
) -> None:
    """When auto-hooks are enabled, a save_as fetch auto-commits to git."""
    from agora.fleet.auto_hooks import run_auto_hooks

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    project_room = await fake_matrix_client.create_room(name="project", topic="")

    commit_calls: list[dict] = []

    class FakeRepo:
        def commit_all(self, message: str) -> str:
            commit_calls.append({"message": message})
            return "deadbeef"

        def stage_changes(self, paths=None) -> list[str]:
            return ["kb/doc.md"]

    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=project_room,
        git_repo=FakeRepo(),
        auto_hooks_enabled=True,
    )
    # The write already happened via fetch_url; here we just exercise the hook.
    (tmp_path / "kb").mkdir()
    (tmp_path / "kb" / "doc.md").write_text("content", encoding="utf-8")

    hooks = await run_auto_hooks(
        call_name="fetch_url",
        call_arguments={"url": "https://x", "save_as": "kb/doc.md"},
        call_result="fetched 7 chars to kb/doc.md",
        ctx=ctx,
    )
    assert any(h.tool_name == "git_commit" for h in hooks)
    assert commit_calls and "kb/doc.md" in commit_calls[0]["message"]
