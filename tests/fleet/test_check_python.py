"""Tests for the ``check_python`` inner tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.core.types import AgentRole
from agora.fleet.inner_tools import ToolContext, get_tool_definitions, get_tool_executor


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


def test_check_python_is_in_filesystem_tool_set() -> None:
    names = {t["name"] for t in get_tool_definitions(AgentRole.IMPLEMENTER)}
    assert "check_python" in names


async def test_check_python_ok_on_valid_file(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "good.py").write_text(
        "import os\n\ndef greet(name: str) -> str:\n    return f'hi {name}'\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["check_python"]({"path": "good.py"})
    assert result.startswith("OK")
    assert "good.py" in result


async def test_check_python_reports_syntax_error(ctx: ToolContext) -> None:
    # Hallucinated `coroutine def` — the exact bug we saw on run 1.
    (Path(ctx.work_dir) / "bad.py").write_text(
        "import discord\n\ncoroutine def ping(interaction):\n    pass\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["check_python"]({"path": "bad.py"})
    assert result.startswith("SyntaxError")
    assert "bad.py" in result


async def test_check_python_reports_missing_colon(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "bad2.py").write_text(
        "def foo()\n    return 1\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["check_python"]({"path": "bad2.py"})
    assert result.startswith("SyntaxError")


async def test_check_python_missing_file(ctx: ToolContext) -> None:
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["check_python"]({"path": "ghost.py"})
    assert result.startswith("ERROR: file not found")


async def test_check_python_blocks_path_traversal(ctx: ToolContext) -> None:
    from agora.core.errors import AgoraError

    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    with pytest.raises(AgoraError, match="escapes work_dir"):
        await executor["check_python"]({"path": "../escape.py"})


async def test_check_python_flags_missing_module_scope_import(ctx: ToolContext) -> None:
    """The exact class of bug from the Discord-bot run: ``os`` used without import."""
    (Path(ctx.work_dir) / "needs_import.py").write_text(
        "import discord\n\nTOKEN = os.environ['DISCORD_TOKEN']\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["check_python"]({"path": "needs_import.py"})
    assert "undefined name" in result
    assert "'os'" in result
    assert "line 3" in result


async def test_check_python_does_not_flag_function_locals(ctx: ToolContext) -> None:
    """Function-body locals must NOT false-positive as undefined module names."""
    (Path(ctx.work_dir) / "fn_locals.py").write_text(
        "def make() -> int:\n"
        "    temp = 1\n"
        "    return temp + 2\n"
        "\n"
        "result = make()\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["check_python"]({"path": "fn_locals.py"})
    assert result.startswith("OK")


async def test_check_python_accepts_builtins_and_dunder_names(ctx: ToolContext) -> None:
    """Builtins (``print``, ``Exception``) and ``__name__`` must not be flagged."""
    (Path(ctx.work_dir) / "dunder.py").write_text(
        "def greet() -> None:\n"
        "    print('hi')\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    greet()\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["check_python"]({"path": "dunder.py"})
    assert result.startswith("OK")


async def test_check_python_accepts_from_import(ctx: ToolContext) -> None:
    """Names bound by ``from X import Y`` and aliased imports must not be flagged."""
    (Path(ctx.work_dir) / "fromimport.py").write_text(
        "from pathlib import Path as P\n"
        "import os.path as op\n"
        "\n"
        "ROOT = P('/tmp')\n"
        "HERE = op.abspath('.')\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["check_python"]({"path": "fromimport.py"})
    assert result.startswith("OK")
