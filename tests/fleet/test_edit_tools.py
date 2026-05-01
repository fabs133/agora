"""Tests for the patch-based edit tools.

Weak LLMs cannot reliably re-emit an existing file's content when asked to make
a small modification (see Run 11: ``discord.Interaction`` → ``discord.InterACTION``
and duplicated ``import random``). These tools let the model emit only the NEW
content; the framework handles the rest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.core.errors import AgoraError
from agora.core.types import AgentRole
from agora.fleet.inner_tools import (
    ToolContext,
    _format_match_locations,
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


# -------------------------------------------------------------- edit_file_replace


async def test_edit_replace_happy_path(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "bot.py").write_text(
        "async def ping(interaction: discord.Interaction):\n    pass\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["edit_file_replace"](
        {
            "path": "bot.py",
            "old_string": "    pass",
            "new_string": "    await interaction.response.send_message('pong')",
        }
    )
    assert result.startswith("replaced ")
    body = (Path(ctx.work_dir) / "bot.py").read_text(encoding="utf-8")
    assert "send_message('pong')" in body
    assert "    pass" not in body


async def test_edit_replace_fails_when_old_string_missing(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "f.py").write_text("hello\n", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="not found"):
        await executor["edit_file_replace"](
            {"path": "f.py", "old_string": "nope", "new_string": "x"}
        )


async def test_edit_replace_fails_when_old_string_not_unique(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "f.py").write_text("x\nx\nx\n", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="matches 3 places"):
        await executor["edit_file_replace"](
            {"path": "f.py", "old_string": "x\n", "new_string": "y\n"}
        )


async def test_edit_replace_multi_match_error_shows_line_numbers(
    ctx: ToolContext,
) -> None:
    """Multi-match error must include line numbers for each match so the
    model can pick disambiguating context without re-reading the file.

    Regression guard for the GPT-4o-mini edit-loop failure mode: without
    location info, the model burns iterations guessing variations of
    old_string, none of which are unique."""
    (Path(ctx.work_dir) / "src.py").write_text(
        "line_a = 1\n"
        "shortener = URLShortener()\n"
        "middle_line = 2\n"
        "url_shortener = URLShortener()\n"  # substring-match trap
        "last_line = 3\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError) as excinfo:
        await executor["edit_file_replace"](
            {
                "path": "src.py",
                "old_string": "shortener = URLShortener()",
                "new_string": "x",
            }
        )
    msg = str(excinfo.value)
    # Both match locations appear with line numbers.
    assert "#1 at line 2" in msg
    assert "#2 at line 4" in msg
    # Match marker lines shown with context (the model sees what's around
    # each match so it can pick distinguishing lines).
    assert "shortener = URLShortener()" in msg
    assert "url_shortener = URLShortener()" in msg


async def test_edit_replace_multi_match_error_includes_upsert_hint(
    ctx: ToolContext,
) -> None:
    """When the file is a Python source and edits are ambiguous, the
    error points the model at add_class_method / add_function as escape
    hatches that upsert by name rather than by whitespace."""
    (Path(ctx.work_dir) / "src.py").write_text("pass\npass\n", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError) as excinfo:
        await executor["edit_file_replace"](
            {"path": "src.py", "old_string": "pass", "new_string": "return"}
        )
    msg = str(excinfo.value)
    assert "add_class_method" in msg
    assert "add_function" in msg


async def test_edit_replace_multi_match_error_caps_shown_matches(
    ctx: ToolContext,
) -> None:
    """When there are many matches, the error caps the rendered list and
    summarises the rest so the error stays readable in context."""
    (Path(ctx.work_dir) / "f.py").write_text("pass\n" * 20, encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError) as excinfo:
        await executor["edit_file_replace"](
            {"path": "f.py", "old_string": "pass", "new_string": "ret"}
        )
    msg = str(excinfo.value)
    # Default max_matches=5 → first 5 shown + "(+K more)" footer.
    assert "#5 at line" in msg
    assert "#6 at line" not in msg
    assert "more match" in msg


async def test_edit_replace_fails_when_old_string_empty(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "f.py").write_text("hello\n", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="must not be empty"):
        await executor["edit_file_replace"](
            {"path": "f.py", "old_string": "", "new_string": "x"}
        )


async def test_edit_replace_missing_file(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["edit_file_replace"](
        {"path": "nope.py", "old_string": "a", "new_string": "b"}
    )
    assert result.startswith("ERROR: file not found")


# -------------------------------------------------------- edit_file_insert_before


async def test_edit_insert_before_inserts_snippet_as_new_block(
    ctx: ToolContext,
) -> None:
    original = (
        "import discord\n"
        "from discord.ext import commands\n"
        "\n"
        "bot = commands.Bot(command_prefix='!')\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    bot.run('TOKEN')\n"
    )
    (Path(ctx.work_dir) / "bot.py").write_text(original, encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["edit_file_insert_before"](
        {
            "path": "bot.py",
            "anchor": "if __name__",
            "snippet": "@bot.tree.command(name='ping')\nasync def ping(i):\n    await i.response.send_message('pong')\n",
        }
    )
    assert result.startswith("inserted ")
    body = (Path(ctx.work_dir) / "bot.py").read_text(encoding="utf-8")
    # Snippet lands on its own lines above the anchor line.
    assert "bot.tree.command" in body
    anchor_idx = body.index("if __name__")
    snippet_idx = body.index("bot.tree.command")
    assert snippet_idx < anchor_idx
    # Original content preserved byte-for-byte.
    assert "import discord" in body
    assert "bot.run('TOKEN')" in body


async def test_edit_insert_before_fails_when_anchor_missing(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "f.py").write_text("hello\n", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="not found"):
        await executor["edit_file_insert_before"](
            {"path": "f.py", "anchor": "goodbye", "snippet": "x\n"}
        )


async def test_edit_insert_before_fails_when_anchor_not_unique(
    ctx: ToolContext,
) -> None:
    (Path(ctx.work_dir) / "f.py").write_text(
        "x\nhello world\nhello again\ndone\n", encoding="utf-8"
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError) as excinfo:
        await executor["edit_file_insert_before"](
            {"path": "f.py", "anchor": "hello", "snippet": "new\n"}
        )
    msg = str(excinfo.value)
    # Base ambiguity message.
    assert "2 lines" in msg
    # Location info — line numbers + the actual matched lines — so the
    # model can pick a longer substring in ONE retry instead of guessing.
    assert "#1 at line 2" in msg
    assert "#2 at line 3" in msg
    assert "hello world" in msg
    assert "hello again" in msg


async def test_edit_insert_before_adds_trailing_newline(ctx: ToolContext) -> None:
    """Snippet without a trailing newline still lands on its own line."""
    (Path(ctx.work_dir) / "f.py").write_text("anchor line\n", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    await executor["edit_file_insert_before"](
        {"path": "f.py", "anchor": "anchor", "snippet": "inserted"}
    )
    body = (Path(ctx.work_dir) / "f.py").read_text(encoding="utf-8")
    assert body == "inserted\nanchor line\n"


# -------------------------------------------------------------- edit_file_append


async def test_edit_append_appends_exactly_once(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "f.py").write_text("head\n", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["edit_file_append"](
        {"path": "f.py", "snippet": "tail\n"}
    )
    assert result.startswith("appended ")
    body = (Path(ctx.work_dir) / "f.py").read_text(encoding="utf-8")
    assert body == "head\ntail\n"


async def test_edit_append_inserts_separator_when_file_missing_trailing_newline(
    ctx: ToolContext,
) -> None:
    (Path(ctx.work_dir) / "f.py").write_text("head", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    await executor["edit_file_append"]({"path": "f.py", "snippet": "tail\n"})
    body = (Path(ctx.work_dir) / "f.py").read_text(encoding="utf-8")
    assert body == "head\ntail\n"


async def test_edit_append_missing_file(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["edit_file_append"](
        {"path": "nope.py", "snippet": "x\n"}
    )
    assert result.startswith("ERROR: file not found")


# ----------------------------------------------------------- cross-cutting guards


@pytest.mark.parametrize(
    "tool,args",
    [
        ("edit_file_replace", {"old_string": "x", "new_string": "y"}),
        ("edit_file_insert_before", {"anchor": "x", "snippet": "y\n"}),
        ("edit_file_append", {"snippet": "y\n"}),
    ],
)
async def test_edit_tools_reject_path_traversal(
    ctx: ToolContext, tool: str, args: dict
) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="escapes work_dir"):
        await executor[tool]({"path": "../escape.py", **args})


async def test_edit_tools_populate_written_files(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "a.py").write_text("anchor line\n", encoding="utf-8")
    (Path(ctx.work_dir) / "b.py").write_text("old\n", encoding="utf-8")
    (Path(ctx.work_dir) / "c.py").write_text("head\n", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    await executor["edit_file_insert_before"](
        {"path": "a.py", "anchor": "anchor", "snippet": "new\n"}
    )
    await executor["edit_file_replace"](
        {"path": "b.py", "old_string": "old\n", "new_string": "new\n"}
    )
    await executor["edit_file_append"]({"path": "c.py", "snippet": "tail\n"})
    assert "a.py" in ctx.written_files
    assert "b.py" in ctx.written_files
    assert "c.py" in ctx.written_files


async def test_edit_tools_log_warning_on_output_path_mismatch(
    tmp_path: Path, fake_matrix_client, caplog
) -> None:
    import logging

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    (tmp_path / "wrong.py").write_text("anchor line\n", encoding="utf-8")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=agent_room,
        expected_output_path="bot.py",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with caplog.at_level(logging.WARNING, logger="agora.fleet.inner_tools"):
        await executor["edit_file_insert_before"](
            {"path": "wrong.py", "anchor": "anchor", "snippet": "new\n"}
        )
    assert any("path mismatch" in rec.message for rec in caplog.records)


def test_edit_tools_are_in_filesystem_tool_set() -> None:
    names = {t["name"] for t in get_tool_definitions(AgentRole.IMPLEMENTER)}
    assert "edit_file_replace" in names
    assert "edit_file_insert_before" in names
    assert "edit_file_append" in names


def test_edit_tools_are_available_to_tester_and_reviewer() -> None:
    for role in (AgentRole.TESTER, AgentRole.REVIEWER):
        names = {t["name"] for t in get_tool_definitions(role)}
        assert "edit_file_replace" in names


def test_edit_tools_hidden_from_architect_by_not_having_filesystem_scope_change() -> None:
    # Architect DOES get filesystem tools today (for writing design/ markdown),
    # so the edit tools are visible there too. This test pins the current
    # behaviour so we notice if role scoping changes.
    names = {t["name"] for t in get_tool_definitions(AgentRole.ARCHITECT)}
    assert "edit_file_replace" in names  # If this flips, reconsider architect's edit capability.


# -------------------------------------------------- auto-hook dispatch integration


async def test_auto_hooks_fire_on_edit_replace_of_py_file(
    tmp_path: Path, fake_matrix_client
) -> None:
    from agora.fleet.auto_hooks import run_auto_hooks

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")

    commits: list[str] = []

    class FakeRepo:
        def commit_all(self, message: str) -> str:
            commits.append(message)
            return "cafef00d"

        def stage_changes(self, paths=None) -> list[str]:
            return ["bot.py"]

    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=agent_room,
        git_repo=FakeRepo(),
        auto_hooks_enabled=True,
    )
    (tmp_path / "bot.py").write_text(
        "import math\nX = math.pi\n", encoding="utf-8"
    )
    hooks = await run_auto_hooks(
        call_name="edit_file_replace",
        call_arguments={
            "path": "bot.py",
            "old_string": "math.pi",
            "new_string": "math.pi",
        },
        call_result="replaced 7 chars in bot.py (delta +0)",
        ctx=ctx,
    )
    names = [h.tool_name for h in hooks]
    assert "check_python" in names
    assert "run_python_import" in names
    assert "git_commit" in names
    assert commits


async def test_auto_hooks_fire_on_edit_insert_before_of_requirements(
    tmp_path: Path, fake_matrix_client
) -> None:
    from agora.fleet.auto_hooks import run_auto_hooks

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")

    class FakeRepo:
        def commit_all(self, message: str) -> str:
            return "deadbeef"

        def stage_changes(self, paths=None) -> list[str]:
            return ["requirements.txt"]

    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=agent_room,
        git_repo=FakeRepo(),
        auto_hooks_enabled=True,
    )
    (tmp_path / "requirements.txt").write_text("discord.py>=2.3\n", encoding="utf-8")
    hooks = await run_auto_hooks(
        call_name="edit_file_insert_before",
        call_arguments={
            "path": "requirements.txt",
            "anchor": "discord",
            "snippet": "aiohttp\n",
        },
        call_result="inserted 8 chars before 'discord' in requirements.txt",
        ctx=ctx,
    )
    names = [h.tool_name for h in hooks]
    assert "check_requirements" in names


async def test_auto_hooks_skip_edit_tools_when_result_is_error(
    tmp_path: Path, fake_matrix_client
) -> None:
    from agora.fleet.auto_hooks import run_auto_hooks

    agent_room = await fake_matrix_client.create_room(name="agent", topic="")
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=fake_matrix_client,
        agent_room_id=agent_room,
        project_room_id=agent_room,
        auto_hooks_enabled=True,
    )
    hooks = await run_auto_hooks(
        call_name="edit_file_replace",
        call_arguments={"path": "missing.py", "old_string": "x", "new_string": "y"},
        call_result="ERROR: file not found: missing.py",
        ctx=ctx,
    )
    assert hooks == []


# --------------------------------------------------------------- fill_test_body (Sprint 7.3)


async def test_fill_test_body_happy_path(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_contract.py").write_text(
        "import pytest\n\n"
        'def test_add():\n    """Add a URL"""\n    pytest.skip("TODO: add")\n',
        encoding="utf-8",
    )
    ctx.active_test_file = "tests/test_contract.py"
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    result = await executor["fill_test_body"](
        {
            "test_name": "test_add",
            "body_code": "from pkg import add\nassert len(add('x')) == 6",
        }
    )
    assert result.startswith("filled body of test_add")
    body = (Path(ctx.work_dir) / "tests" / "test_contract.py").read_text(
        encoding="utf-8"
    )
    assert "pytest.skip(\"TODO: add\")" not in body
    assert "from pkg import add" in body
    assert "assert len(add('x')) == 6" in body
    assert "\"\"\"Add a URL\"\"\"" in body


async def test_fill_test_body_errors_without_binding(ctx: ToolContext) -> None:
    """If active_test_file is empty, tool refuses to run."""
    ctx.active_test_file = ""
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    with pytest.raises(AgoraError, match="no active test file"):
        await executor["fill_test_body"](
            {"test_name": "test_x", "body_code": "assert True"}
        )


async def test_fill_test_body_missing_function(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_contract.py").write_text(
        "import pytest\n\ndef test_x():\n    pytest.skip('TODO')\n",
        encoding="utf-8",
    )
    ctx.active_test_file = "tests/test_contract.py"
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    with pytest.raises(AgoraError, match="no function named"):
        await executor["fill_test_body"](
            {"test_name": "test_nonexistent", "body_code": "assert True"}
        )


async def test_fill_test_body_invalid_python_body(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_contract.py").write_text(
        "import pytest\n\ndef test_x():\n    pytest.skip('TODO')\n",
        encoding="utf-8",
    )
    ctx.active_test_file = "tests/test_contract.py"
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    with pytest.raises(AgoraError, match="invalid python"):
        await executor["fill_test_body"](
            {"test_name": "test_x", "body_code": "def foo(\n"}
        )


async def test_fill_test_body_tolerates_overindented_body(ctx: ToolContext) -> None:
    """7B frequently over-indents; the framework normalises to 4 spaces."""
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_contract.py").write_text(
        "import pytest\n\n"
        'def test_x():\n    """doc"""\n    pytest.skip("TODO")\n',
        encoding="utf-8",
    )
    ctx.active_test_file = "tests/test_contract.py"
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    await executor["fill_test_body"](
        {
            "test_name": "test_x",
            "body_code": "            from pkg import thing\n            assert thing() is not None",
        }
    )
    body = (Path(ctx.work_dir) / "tests" / "test_contract.py").read_text(
        encoding="utf-8"
    )
    # Body lands at 4-space indent inside def test_x().
    assert "    from pkg import thing" in body
    assert "    assert thing() is not None" in body
    # Still parseable.
    import ast
    ast.parse(body)


def test_manifest_hides_fill_test_body_when_unbound() -> None:
    """Manifest shape: tool hidden unless fill_test_body_bound=True."""
    tools = get_tool_definitions(
        AgentRole.TESTER,
        auto_hooks_enabled=False,
        plan_authoring_enabled=False,
        fill_test_body_bound=False,
    )
    names = {t["name"] for t in tools}
    assert "fill_test_body" not in names


def test_manifest_exposes_fill_test_body_when_bound() -> None:
    tools = get_tool_definitions(
        AgentRole.TESTER,
        auto_hooks_enabled=False,
        plan_authoring_enabled=False,
        fill_test_body_bound=True,
    )
    names = {t["name"] for t in tools}
    assert "fill_test_body" in names


# ==================================================== role-path scope (Sprint 7.3)


async def test_implementer_cannot_edit_test_files(ctx: ToolContext) -> None:
    """Observed failure: implementer 'fixed' failing contract tests by
    editing tests/test_contract.py. Framework now rejects this."""
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_contract.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="implementer role may not write"):
        await executor["edit_file_replace"](
            {"path": "tests/test_contract.py",
             "old_string": "assert True",
             "new_string": "assert False"}
        )


async def test_implementer_cannot_write_to_tests_dir(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="may not write"):
        await executor["write_file"](
            {"path": "tests/test_new.py", "content": "def test_x(): pass\n"}
        )


async def test_tester_cannot_edit_src_files(ctx: ToolContext) -> None:
    """Symmetric: tester can't 'fix' failing tests by rewriting implementation."""
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "domain.py").write_text(
        "def add(x, y): return x + y\n", encoding="utf-8"
    )
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    with pytest.raises(AgoraError, match="tester role may not write"):
        await executor["edit_file_replace"](
            {"path": "src/domain.py",
             "old_string": "return x + y",
             "new_string": "return x * y"}
        )


async def test_architect_can_write_anywhere(ctx: ToolContext) -> None:
    """Architect is broad (plan-builder authors plan/**)."""
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "src").mkdir()
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    await executor["write_file"](
        {"path": "tests/test_x.py", "content": "def test(): pass\n"}
    )
    await executor["write_file"](
        {"path": "src/thing.py", "content": "x = 1\n"}
    )


async def test_implementer_can_still_write_to_src(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["write_file"](
        {"path": "src/thing.py", "content": "x = 1\n"}
    )
    assert "wrote" in result


async def test_tester_can_still_write_to_tests(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    result = await executor["write_file"](
        {"path": "tests/test_x.py", "content": "def test(): pass\n"}
    )
    assert "wrote" in result


# ==================================================== add_function / add_class / add_class_method (Sprint 7.4)


async def test_add_function_creates_new_file(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["add_function"](
        {
            "path": "src/util.py",
            "code": "def shorten(url):\n    return url[:6]\n",
        }
    )
    assert "upserted function 'shorten'" in result
    body = (Path(ctx.work_dir) / "src" / "util.py").read_text(encoding="utf-8")
    assert "def shorten(url):" in body
    import ast
    ast.parse(body)


async def test_add_function_replaces_existing(ctx: ToolContext) -> None:
    """Called twice with the same name → ONE definition in the file."""
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "util.py").write_text(
        "def other():\n    pass\n\n"
        "def target():\n    return 'old'\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    await executor["add_function"](
        {"path": "src/util.py", "code": "def target():\n    return 'new'\n"}
    )
    body = (Path(ctx.work_dir) / "src" / "util.py").read_text(encoding="utf-8")
    assert body.count("def target(") == 1
    assert "return 'new'" in body
    assert "return 'old'" not in body


async def test_add_function_rejects_non_py_path(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="must be a .py file"):
        await executor["add_function"](
            {"path": "src/notes.md", "code": "def x(): pass\n"}
        )


async def test_add_function_scope_gated_for_tester(ctx: ToolContext) -> None:
    """Tester can't use add_function to write src/ code."""
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    with pytest.raises(AgoraError, match="tester role may not write"):
        await executor["add_function"](
            {"path": "src/util.py", "code": "def x(): pass\n"}
        )


async def test_add_class_creates_then_add_class_method_appends(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    await executor["add_class"](
        {
            "path": "src/domain.py",
            "code": (
                "class URLShortener:\n"
                "    def __init__(self):\n"
                "        self.mappings = {}\n"
            ),
        }
    )
    await executor["add_class_method"](
        {
            "path": "src/domain.py",
            "class_name": "URLShortener",
            "code": "def shorten(self, url):\n    return url[:6]\n",
        }
    )
    await executor["add_class_method"](
        {
            "path": "src/domain.py",
            "class_name": "URLShortener",
            "code": "def lookup(self, hash):\n    return self.mappings.get(hash)\n",
        }
    )
    body = (Path(ctx.work_dir) / "src" / "domain.py").read_text(encoding="utf-8")
    import ast
    tree = ast.parse(body)
    cls = tree.body[0]
    assert isinstance(cls, ast.ClassDef) and cls.name == "URLShortener"
    method_names = [
        n.name for n in cls.body
        if isinstance(n, ast.FunctionDef)
    ]
    assert method_names == ["__init__", "shorten", "lookup"]


async def test_add_class_method_idempotent_no_duplicates(ctx: ToolContext) -> None:
    """Regression guard: the accumulation problem Sprint 7.4 exists to prevent."""
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    await executor["add_class"](
        {
            "path": "src/domain.py",
            "code": "class Store:\n    pass\n",
        }
    )
    # Call the same upsert 5 times with slightly different bodies — each
    # should REPLACE, not append.
    for i in range(5):
        await executor["add_class_method"](
            {
                "path": "src/domain.py",
                "class_name": "Store",
                "code": f"def put(self, k, v):\n    self.version = {i}\n",
            }
        )
    body = (Path(ctx.work_dir) / "src" / "domain.py").read_text(encoding="utf-8")
    # Only ONE def put, and it holds the last call's body.
    assert body.count("def put(") == 1
    assert "self.version = 4" in body
    import ast
    ast.parse(body)


async def test_add_class_method_errors_when_class_absent(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="class 'URLShortener' not found"):
        await executor["add_class_method"](
            {
                "path": "src/foo.py",
                "class_name": "URLShortener",
                "code": "def m(self): pass\n",
            }
        )


async def test_add_class_method_errors_when_file_absent(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="does not exist"):
        await executor["add_class_method"](
            {
                "path": "src/missing.py",
                "class_name": "X",
                "code": "def m(self): pass\n",
            }
        )


async def test_add_function_rejects_multi_def_code(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="exactly one function"):
        await executor["add_function"](
            {
                "path": "src/x.py",
                "code": "def a(): pass\ndef b(): pass\n",
            }
        )


async def test_add_function_rejects_invalid_python(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="invalid python"):
        await executor["add_function"](
            {"path": "src/x.py", "code": "def broken(\n"}
        )


# ======================================================= Sprint 7.6(e) auto-route self-fns


async def test_add_function_autoroutes_self_to_class_method(ctx: ToolContext) -> None:
    """When the file has exactly ONE class and the code has `self` as first
    arg, add_function upserts as a method of that class instead of dumping
    a top-level duplicate."""
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "thing.py").write_text(
        '"""Implementation stubs — bodies filled in by implementer."""\n\n'
        "class Thing:\n"
        "    def __init__(self) -> None:\n"
        "        raise NotImplementedError\n"
        "    def do_it(self) -> str:\n"
        "        raise NotImplementedError\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["add_function"](
        {
            "path": "src/thing.py",
            "code": "def do_it(self) -> str:\n    return 'real'\n",
        }
    )
    assert "auto-routed" in result
    assert "Thing" in result
    body = (Path(ctx.work_dir) / "src" / "thing.py").read_text(encoding="utf-8")
    # Method body replaced, NO top-level duplicate.
    assert body.count("def do_it(") == 1
    import ast
    tree = ast.parse(body)
    cls = [n for n in tree.body if isinstance(n, ast.ClassDef)][0]
    method = [
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "do_it"
    ][0]
    # Real body, not stub.
    src = ast.unparse(method)
    assert "return 'real'" in src


async def test_add_function_rejects_self_when_ambiguous_classes(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "thing.py").write_text(
        "class A:\n    pass\n\nclass B:\n    pass\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="METHOD"):
        await executor["add_function"](
            {
                "path": "src/thing.py",
                "code": "def m(self) -> None: pass\n",
            }
        )


async def test_add_function_rejects_self_when_no_class(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="METHOD"):
        await executor["add_function"](
            {
                "path": "src/mod.py",
                "code": "def m(self) -> None: pass\n",
            }
        )


async def test_add_function_allows_normal_function_without_self(ctx: ToolContext) -> None:
    """Sanity: self-detection doesn't break normal add_function usage."""
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["add_function"](
        {
            "path": "src/util.py",
            "code": "def helper(x: int) -> int:\n    return x + 1\n",
        }
    )
    assert "upserted function 'helper'" in result


# ==================================================== Sprint 7.6(f) block edits on stubs


async def test_edit_file_replace_rejected_on_stub(ctx: ToolContext) -> None:
    """Stubs must be mutated via add_class_method / add_function upserts.
    edit_file_replace on a stub file produces nested defs / orphan returns."""
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "thing.py").write_text(
        '"""Implementation stubs."""\n\n'
        "class Thing:\n"
        "    def do(self) -> None:\n"
        "        raise NotImplementedError\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="stub"):
        await executor["edit_file_replace"](
            {
                "path": "src/thing.py",
                "old_string": "raise NotImplementedError",
                "new_string": "return None",
            }
        )


async def test_edit_file_append_rejected_on_stub(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "thing.py").write_text(
        "class Thing:\n    def do(self):\n        raise NotImplementedError\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="stub"):
        await executor["edit_file_append"](
            {"path": "src/thing.py", "snippet": "# trailing comment\n"}
        )


async def test_edit_file_insert_before_rejected_on_stub(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "thing.py").write_text(
        "class Thing:\n    def do(self):\n        raise NotImplementedError\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="stub"):
        await executor["edit_file_insert_before"](
            {"path": "src/thing.py", "anchor": "class Thing:", "snippet": "import os\n"}
        )


async def test_edit_file_replace_allowed_after_stub_filled(ctx: ToolContext) -> None:
    """Once all NotImplementedError markers are gone, edit tools work again."""
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "thing.py").write_text(
        "class Thing:\n    def do(self) -> str:\n        return 'real'\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["edit_file_replace"](
        {
            "path": "src/thing.py",
            "old_string": "return 'real'",
            "new_string": "return 'final'",
        }
    )
    assert "replaced" in result


async def test_edit_file_replace_allowed_on_non_src_files(ctx: ToolContext) -> None:
    """The stub guard only applies to src/*.py — test files + docs unaffected."""
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_x.py").write_text(
        "def test_x():\n    raise NotImplementedError\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    result = await executor["edit_file_replace"](
        {
            "path": "tests/test_x.py",
            "old_string": "raise NotImplementedError",
            "new_string": "assert True",
        }
    )
    assert "replaced" in result


# ================================================== Sprint 7.6(g) spec-aware name guard


async def test_fill_test_body_rejects_unknown_method_name(ctx: ToolContext) -> None:
    """Regression of the 7.5d/7.6 failure: tester wrote
    `URLShortener().add(url)` when spec declares `add_url`."""
    # Seed api_spec.
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/url_shortener.py\n\n"
        "class URLShortener:\n"
        "    def __init__(self) -> None: ...\n"
        "    def add_url(self, url: str) -> str: ...\n",
        encoding="utf-8",
    )
    # Seed scaffolded test file.
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_contract.py").write_text(
        "import pytest\n"
        "from src.url_shortener import URLShortener\n\n"
        'def test_add():\n    """doc"""\n    pytest.skip("TODO")\n',
        encoding="utf-8",
    )
    ctx.active_test_file = "tests/test_contract.py"
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    with pytest.raises(AgoraError, match="URLShortener.add"):
        await executor["fill_test_body"](
            {
                "test_name": "test_add",
                "body_code": (
                    "from src.url_shortener import URLShortener\n"
                    "result = URLShortener().add('https://example.com')\n"
                    "assert len(result) == 6\n"
                ),
            }
        )


async def test_fill_test_body_accepts_known_method_name(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/url_shortener.py\n\n"
        "class URLShortener:\n"
        "    def add_url(self, url: str) -> str: ...\n",
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_contract.py").write_text(
        "import pytest\n"
        "from src.url_shortener import URLShortener\n\n"
        'def test_add():\n    """doc"""\n    pytest.skip("TODO")\n',
        encoding="utf-8",
    )
    ctx.active_test_file = "tests/test_contract.py"
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    result = await executor["fill_test_body"](
        {
            "test_name": "test_add",
            "body_code": (
                "from src.url_shortener import URLShortener\n"
                "result = URLShortener().add_url('https://example.com')\n"
                "assert len(result) == 6\n"
            ),
        }
    )
    assert "filled body" in result


async def test_fill_test_body_no_spec_file_is_permissive(ctx: ToolContext) -> None:
    """When api_spec.md doesn't exist, validator is a no-op (pre-7.5 plans)."""
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_x.py").write_text(
        "import pytest\n"
        'def test_x():\n    """doc"""\n    pytest.skip("TODO")\n',
        encoding="utf-8",
    )
    ctx.active_test_file = "tests/test_x.py"
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    result = await executor["fill_test_body"](
        {
            "test_name": "test_x",
            "body_code": "Anything().whatever()\nassert True\n",
        }
    )
    assert "filled body" in result


async def test_add_class_method_rejects_unknown_method_on_spec_class(
    ctx: ToolContext,
) -> None:
    """Symmetric guard on implementer path."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/lib.py\n\n"
        "class Foo:\n"
        "    def known(self) -> None: ...\n"
        "\n"
        "class Bar:\n"
        "    def other(self) -> None: ...\n",
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "lib.py").write_text(
        "class Foo:\n    def known(self) -> None: pass\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="Bar.does_not_exist"):
        # Impl method body calls Bar().does_not_exist() — not in spec.
        await executor["add_class_method"](
            {
                "path": "src/lib.py",
                "class_name": "Foo",
                "code": (
                    "def known(self) -> None:\n"
                    "    from src.lib import Bar\n"
                    "    Bar().does_not_exist()\n"
                ),
            }
        )


# =================================================== Sprint 7.7(h)(i) spec-lock names


async def test_add_class_rejects_class_not_in_spec(ctx: ToolContext) -> None:
    """Observed failure: implementer kept creating parallel off-spec classes
    (ShortUrlRepository, UrlShortener) alongside URLShortener.
    Framework now rejects those."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/app.py\n\n"
        "class URLShortener:\n"
        "    def add_url(self, url: str) -> str: ...\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="NOT in plan/api_spec.md"):
        await executor["add_class"](
            {
                "path": "src/app.py",
                "code": "class ShortUrlRepository:\n    def foo(self): pass\n",
            }
        )


async def test_add_class_accepts_spec_class(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/app.py\n\nclass URLShortener:\n    def add_url(self, url: str) -> str: ...\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["add_class"](
        {
            "path": "src/app.py",
            "code": "class URLShortener:\n    def add_url(self, url: str) -> str:\n        return url[:6]\n",
        }
    )
    assert "upserted class 'URLShortener'" in result


async def test_add_class_rejected_when_spec_has_no_classes(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/util.py\n\ndef helper(x: int) -> int: ...\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="declares no classes"):
        await executor["add_class"](
            {
                "path": "src/util.py",
                "code": "class Something:\n    pass\n",
            }
        )


async def test_add_class_permissive_without_spec(ctx: ToolContext) -> None:
    """Back-compat: no plan/api_spec.md → no validation."""
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["add_class"](
        {
            "path": "src/anything.py",
            "code": "class AnyClass:\n    pass\n",
        }
    )
    assert "upserted class 'AnyClass'" in result


async def test_add_class_method_rejects_class_not_in_spec(ctx: ToolContext) -> None:
    """Regression guard for the back-door case: implementer can't add methods
    to off-spec classes either."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/app.py\n\nclass URLShortener:\n    def x(self): ...\n",
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "app.py").write_text(
        "class URLShortener:\n    def x(self): pass\n\n"
        "class OffSpec:\n    pass\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="OffSpec"):
        await executor["add_class_method"](
            {
                "path": "src/app.py",
                "class_name": "OffSpec",
                "code": "def foo(self): pass\n",
            }
        )


async def test_add_function_rejects_name_not_in_spec(ctx: ToolContext) -> None:
    """Observed failure: implementer made up top-level function names.
    Public functions must match the spec."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/util.py\n\ndef helper(x: int) -> int: ...\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="NOT in plan/api_spec.md"):
        await executor["add_function"](
            {
                "path": "src/util.py",
                "code": "def invented(x: int) -> int:\n    return x\n",
            }
        )


async def test_add_function_accepts_spec_name(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/util.py\n\ndef helper(x: int) -> int: ...\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["add_function"](
        {
            "path": "src/util.py",
            "code": "def helper(x: int) -> int:\n    return x + 1\n",
        }
    )
    assert "upserted function 'helper'" in result


async def test_add_function_allows_private_helpers(ctx: ToolContext) -> None:
    """Underscore-prefixed names bypass the spec check — internal helpers ok."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/util.py\n\ndef helper(x: int) -> int: ...\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["add_function"](
        {
            "path": "src/util.py",
            "code": "def _internal(x: int) -> int:\n    return x * 2\n",
        }
    )
    assert "upserted function '_internal'" in result


async def test_add_function_rejected_when_spec_classes_only(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/app.py\n\nclass URLShortener:\n    def x(self): ...\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="declares no top-level functions"):
        await executor["add_function"](
            {
                "path": "src/app.py",
                "code": "def random_function() -> None: pass\n",
            }
        )


# ========================================= method-at-definition lock (Sprint 7.7 follow-up)


async def test_add_class_method_rejects_method_name_not_in_spec(ctx: ToolContext) -> None:
    """Observed: implementer defined save_mapping / get_mapping on URLShortener
    (spec has add_url / lookup_hash). Tests then failed with AttributeError."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/app.py\n\n"
        "class URLShortener:\n"
        "    def add_url(self, url: str) -> str: ...\n"
        "    def lookup_hash(self, h: str) -> str: ...\n",
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "app.py").write_text(
        "class URLShortener:\n    pass\n", encoding="utf-8"
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="save_mapping"):
        await executor["add_class_method"](
            {
                "path": "src/app.py",
                "class_name": "URLShortener",
                "code": "def save_mapping(self, url: str) -> str:\n    return url[:6]\n",
            }
        )


async def test_add_class_method_accepts_spec_method(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/app.py\n\n"
        "class URLShortener:\n"
        "    def add_url(self, url: str) -> str: ...\n",
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "app.py").write_text(
        "class URLShortener:\n    pass\n", encoding="utf-8"
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["add_class_method"](
        {
            "path": "src/app.py",
            "class_name": "URLShortener",
            "code": "def add_url(self, url: str) -> str:\n    return url[:6]\n",
        }
    )
    assert "upserted method URLShortener.add_url" in result


async def test_add_class_method_allows_dunder_and_private(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/app.py\n\nclass Foo:\n    def public(self): ...\n",
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "app.py").write_text(
        "class Foo:\n    pass\n", encoding="utf-8"
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    # Dunder allowed even if spec doesn't list __init__.
    await executor["add_class_method"](
        {
            "path": "src/app.py",
            "class_name": "Foo",
            "code": "def __init__(self) -> None:\n    self.x = 0\n",
        }
    )
    # Private helper allowed.
    await executor["add_class_method"](
        {
            "path": "src/app.py",
            "class_name": "Foo",
            "code": "def _helper(self) -> int:\n    return 1\n",
        }
    )


async def test_add_function_autoroute_rejects_unknown_method(ctx: ToolContext) -> None:
    """When add_function gets self-first code and auto-routes, the
    method-name validation still fires. Observed failure path: model calls
    add_function with `def save_mapping(self, ...)` on a file with URLShortener
    class — auto-router used to silently upsert as URLShortener.save_mapping."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/app.py\n\n"
        "class URLShortener:\n"
        "    def add_url(self, url: str) -> str: ...\n",
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "app.py").write_text(
        "class URLShortener:\n    pass\n", encoding="utf-8"
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="save_mapping"):
        await executor["add_function"](
            {
                "path": "src/app.py",
                "code": "def save_mapping(self, url: str) -> str:\n    return url[:6]\n",
            }
        )


# ============================================ retry-wipe guard (Sprint 7.7 follow-up)


async def test_add_class_refuses_to_wipe_filled_methods(ctx: ToolContext) -> None:
    """Observed: task retry called add_class which upsert-replaced the whole
    class, nuking already-filled method bodies from the first attempt."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/app.py\n\n"
        "class URLShortener:\n"
        "    def add_url(self, url: str) -> str: ...\n",
        encoding="utf-8",
    )
    # Simulate first-attempt success: URLShortener has real add_url body.
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "app.py").write_text(
        "class URLShortener:\n"
        "    def add_url(self, url: str) -> str:\n"
        "        return url[:6]\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    # Retry tries to wholesale-replace the class with just a stub.
    with pytest.raises(AgoraError, match="already exists.*filled methods"):
        await executor["add_class"](
            {
                "path": "src/app.py",
                "code": "class URLShortener:\n    def __init__(self):\n        pass\n",
            }
        )


async def test_add_class_allowed_when_all_methods_are_stubs(ctx: ToolContext) -> None:
    """On the INITIAL add_class (against the seeded stub file where every
    body is raise NotImplementedError), overwrite is fine — no progress to
    protect."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/app.py\n\nclass URLShortener:\n    def add_url(self, url: str) -> str: ...\n",
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "src").mkdir()
    (Path(ctx.work_dir) / "src" / "app.py").write_text(
        "from __future__ import annotations\n\n"
        "class URLShortener:\n"
        "    def add_url(self, url: str) -> str:\n"
        "        raise NotImplementedError\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    # All-stub methods → no retry-wipe risk, allowed.
    result = await executor["add_class"](
        {
            "path": "src/app.py",
            "code": (
                "class URLShortener:\n"
                "    def add_url(self, url: str) -> str:\n"
                "        return url[:6]\n"
            ),
        }
    )
    assert "upserted class 'URLShortener'" in result


async def test_stubs_get_future_annotations_import(tmp_path: Path):
    """Sprint 7.7 follow-up: seeded stubs get `from __future__ import annotations`
    so implementer can use Optional/List without module-level typing imports."""
    from agora.plan.api_spec import parse_api_spec, render_impl_stub

    modules = parse_api_spec(
        "## module: src/app.py\n\n"
        "class URLShortener:\n"
        "    def lookup(self, h: str) -> Optional[str]: ...\n"
    )
    stub = render_impl_stub(modules[0])
    assert "from __future__ import annotations" in stub
    # And parses despite the unresolved Optional name.
    import ast
    ast.parse(stub)


# =============================================================
# _format_match_locations — diagnostic helper for edit-tool errors
# =============================================================


def test_format_match_locations_single_match_shows_marker_and_context():
    body = "alpha\nbeta\ngamma\ndelta\n"
    out = _format_match_locations(body, "gamma")
    # Marker arrow on the match line + ±1 line context.
    assert "→ L  3: gamma" in out
    assert "L  2: beta" in out
    assert "L  4: delta" in out
    # Only one match reported.
    assert "#1 at line 3" in out
    assert "#2" not in out


def test_format_match_locations_multiple_matches_enumerated():
    body = "x\nx\nx\n"
    out = _format_match_locations(body, "x\n")
    assert "#1 at line 1" in out
    assert "#2 at line 2" in out
    assert "#3 at line 3" in out


def test_format_match_locations_substring_overlap():
    """Catches the exact failure from the GPT-4o-mini executor run:
    ``shortener = ...`` matches inside ``url_shortener = ...`` too."""
    body = (
        "shortener = URLShortener()\n"
        "other_line = 1\n"
        "url_shortener = URLShortener()\n"
    )
    out = _format_match_locations(body, "shortener = URLShortener()")
    # Both matches rendered — one on line 1, one on line 3 (inside url_shortener).
    assert "#1 at line 1" in out
    assert "#2 at line 3" in out


def test_format_match_locations_caps_at_max_matches():
    body = "x\n" * 10
    out = _format_match_locations(body, "x\n", max_matches=3)
    assert "#1 at line 1" in out
    assert "#3 at line 3" in out
    assert "#4 at line 4" not in out
    # Footer summarises the remainder.
    assert "(+7 more match(es) not shown)" in out


def test_format_match_locations_empty_needle_returns_empty():
    assert _format_match_locations("body", "") == ""


def test_format_match_locations_no_matches_returns_empty():
    assert _format_match_locations("alpha\nbeta\n", "gamma") == ""


def test_format_match_locations_at_file_boundaries():
    """First-line and last-line matches must not crash — the context
    window clamps to the file's edges."""
    body = "target\nmiddle\ntarget\n"
    out = _format_match_locations(body, "target")
    # First line: no "before" context line exists.
    assert "→ L  1: target" in out
    # Last line: no "after" context line exists.
    assert "→ L  3: target" in out


# ================================================================
# v2.9 Phase 2: fill_test_body rejects return-type drift
# ================================================================


async def test_fill_test_body_rejects_string_subscript_on_list_str_return(
    ctx: ToolContext,
) -> None:
    """Regression guard for the 2026-04-22 failure: tester wrote
    ``mappings[0]['hash']`` on a ``list[str]`` return. Must be caught
    as a certain violation by the L1↔L2 structural check."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/url_shortener.py\n\n"
        "class URLShortener:\n"
        "    def __init__(self) -> None: ...\n"
        "    def list_mappings(self) -> list[str]: ...\n",
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_contract.py").write_text(
        "import pytest\n"
        "from src.url_shortener import URLShortener\n\n"
        'def test_x():\n    """doc"""\n    pytest.skip("TODO")\n',
        encoding="utf-8",
    )
    ctx.active_test_file = "tests/test_contract.py"
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    with pytest.raises(AgoraError) as excinfo:
        await executor["fill_test_body"](
            {
                "test_name": "test_x",
                "body_code": (
                    "from src.url_shortener import URLShortener\n"
                    "s = URLShortener()\n"
                    "mappings = s.list_mappings()\n"
                    "assert all(m['hash'] for m in mappings)\n"
                ),
            }
        )
    msg = str(excinfo.value)
    # Error names the offending chain + the underlying type mismatch.
    assert "list_mappings" in msg
    assert "string key" in msg.lower() or "int" in msg.lower()


async def test_fill_test_body_accepts_consistent_usage(
    ctx: ToolContext,
) -> None:
    """Sanity: a test body that uses method returns consistently with
    the spec's declared types still goes through."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/url_shortener.py\n\n"
        "class URLShortener:\n"
        "    def add_url(self, url: str) -> str: ...\n"
        "    def list_mappings(self) -> list[tuple[str, str]]: ...\n",
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_contract.py").write_text(
        "import pytest\n"
        "from src.url_shortener import URLShortener\n\n"
        'def test_x():\n    """doc"""\n    pytest.skip("TODO")\n',
        encoding="utf-8",
    )
    ctx.active_test_file = "tests/test_contract.py"
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    result = await executor["fill_test_body"](
        {
            "test_name": "test_x",
            "body_code": (
                "from src.url_shortener import URLShortener\n"
                "s = URLShortener()\n"
                "h = s.add_url('http://a')\n"
                "assert isinstance(h, str)\n"
                "mappings = s.list_mappings()\n"
                "first_pair = mappings[0]\n"
                "assert first_pair[0] == 'http://a'\n"
            ),
        }
    )
    assert "filled body" in result


async def test_fill_test_body_permissive_allows_unresolved_types(
    ctx: ToolContext,
) -> None:
    """Unannotated methods → Unknown return type. Permissive mode
    lets through any access on them (they could be anything)."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/mystery.py\n\n"
        "class Mystery:\n"
        "    def oracle(self): ...\n",   # no return annotation
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_contract.py").write_text(
        "import pytest\n"
        "from src.mystery import Mystery\n\n"
        'def test_x():\n    """doc"""\n    pytest.skip("TODO")\n',
        encoding="utf-8",
    )
    ctx.active_test_file = "tests/test_contract.py"
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    # Deeply-nested access on an Unknown return — not flagged in
    # permissive mode.
    result = await executor["fill_test_body"](
        {
            "test_name": "test_x",
            "body_code": (
                "from src.mystery import Mystery\n"
                "m = Mystery().oracle()\n"
                "assert m[0]['key'].upper() == 'X'\n"
            ),
        }
    )
    assert "filled body" in result


async def test_fill_test_body_strict_mode_flags_unresolved(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGORA_STRUCTURE_STRICT=1 turns unresolved warnings into rejections."""
    (Path(ctx.work_dir) / "plan").mkdir()
    (Path(ctx.work_dir) / "plan" / "api_spec.md").write_text(
        "## module: src/mystery.py\n\n"
        "class Mystery:\n"
        "    def oracle(self): ...\n",
        encoding="utf-8",
    )
    (Path(ctx.work_dir) / "tests").mkdir()
    (Path(ctx.work_dir) / "tests" / "test_contract.py").write_text(
        "import pytest\n"
        "from src.mystery import Mystery\n\n"
        'def test_x():\n    """doc"""\n    pytest.skip("TODO")\n',
        encoding="utf-8",
    )
    ctx.active_test_file = "tests/test_contract.py"
    monkeypatch.setenv("AGORA_STRUCTURE_STRICT", "1")
    executor = get_tool_executor(AgentRole.TESTER, ctx)
    with pytest.raises(AgoraError, match="unresolved"):
        await executor["fill_test_body"](
            {
                "test_name": "test_x",
                "body_code": (
                    "from src.mystery import Mystery\n"
                    "m = Mystery().oracle()\n"
                    "assert m[0]['key'] == 'X'\n"
                ),
            }
        )
