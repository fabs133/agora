"""Tests for the runtime tool factories.

Runtime tools are not exposed to any LLM role — they exist only to share code
with the postcondition helpers. The tests exercise the factory callables
directly to keep the factories covered and to verify their contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.core.types import AgentRole
from agora.fleet.inner_tools import (
    ToolContext,
    _make_check_requirements,
    _make_run_pytest,
    _make_run_python_import,
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
    )


# ------------------------------------------------------------ tool-set guards


def test_runtime_tools_are_not_exposed_to_any_role() -> None:
    """Runtime tools (subprocess-execution) are postcondition-only — agents
    never see them. Exposing them to the LLM caused a model-behaviour
    regression in Run 3 (agents over-engineered their code in response to the
    multi-step instructions)."""
    for role in AgentRole:
        names = {t["name"] for t in get_tool_definitions(role)}
        assert "run_python_import" not in names
        assert "run_pytest" not in names
        assert "check_requirements" not in names


# --------------------------------------------------------- run_python_import


async def test_run_python_import_ok_on_clean_module(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "good.py").write_text(
        "import math\nX = math.pi\n", encoding="utf-8"
    )
    result = await _make_run_python_import(ctx)({"path": "good.py"})
    assert result.startswith("OK")
    assert "good.py" in result


async def test_run_python_import_surfaces_nameerror(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "bad.py").write_text(
        "x = undefined_symbol\n", encoding="utf-8"
    )
    result = await _make_run_python_import(ctx)({"path": "bad.py"})
    assert result.startswith("IMPORT FAILED")
    assert "NameError" in result


async def test_run_python_import_surfaces_attributeerror(ctx: ToolContext) -> None:
    """The Run-2 hallucination class: attribute access at module scope."""
    (Path(ctx.work_dir) / "hallucinated.py").write_text(
        "class _Mod:\n"
        "    pass\n"
        "mod = _Mod()\n"
        "X = mod.random.randint(1, 6)\n",
        encoding="utf-8",
    )
    result = await _make_run_python_import(ctx)({"path": "hallucinated.py"})
    assert result.startswith("IMPORT FAILED")
    assert "AttributeError" in result


async def test_run_python_import_survives_missing_env(ctx: ToolContext) -> None:
    """Module that reads DISCORD_TOKEN at module scope still imports."""
    (Path(ctx.work_dir) / "env.py").write_text(
        "import os\nTOKEN = os.environ['DISCORD_TOKEN']\n", encoding="utf-8"
    )
    result = await _make_run_python_import(ctx)({"path": "env.py"})
    assert result.startswith("OK")


async def test_run_python_import_missing_file(ctx: ToolContext) -> None:
    result = await _make_run_python_import(ctx)({"path": "ghost.py"})
    assert result.startswith("ERROR: file not found")


async def test_run_python_import_blocks_path_traversal(ctx: ToolContext) -> None:
    from agora.core.errors import AgoraError

    with pytest.raises(AgoraError, match="escapes work_dir"):
        await _make_run_python_import(ctx)({"path": "../escape.py"})


# ------------------------------------------------------------------ run_pytest


async def test_run_pytest_ok_on_passing_suite(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "test_ok.py").write_text(
        "def test_math():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    result = await _make_run_pytest(ctx)({"path": "test_ok.py"})
    assert result.startswith("OK")


async def test_run_pytest_reports_failure(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "test_bad.py").write_text(
        "def test_no():\n    assert False\n", encoding="utf-8"
    )
    result = await _make_run_pytest(ctx)({"path": "test_bad.py"})
    assert result.startswith("PYTEST FAILED")
    assert "test_bad" in result


async def test_run_pytest_surfaces_command_not_callable(ctx: ToolContext) -> None:
    """Regression for Run-2: `fn(...)` where fn is a non-callable Command."""
    (Path(ctx.work_dir) / "test_cmd.py").write_text(
        "class Command:\n"
        "    pass\n"
        "\n"
        "ping = Command()\n"
        "\n"
        "def test_ping():\n"
        "    ping(None)\n",
        encoding="utf-8",
    )
    result = await _make_run_pytest(ctx)({"path": "test_cmd.py"})
    assert result.startswith("PYTEST FAILED")
    assert "not callable" in result or "TypeError" in result


async def test_run_pytest_missing_file(ctx: ToolContext) -> None:
    result = await _make_run_pytest(ctx)({"path": "nosuchfile.py"})
    assert result.startswith("ERROR: not found")


# --------------------------------------------------------- check_requirements


async def test_check_requirements_ok(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "requirements.txt").write_text(
        "discord.py>=2.3\nrequests\n# a comment\n\n", encoding="utf-8"
    )
    result = await _make_check_requirements(ctx)({})
    assert result.startswith("OK")
    assert "2 requirements" in result


async def test_check_requirements_flags_import_line(ctx: ToolContext) -> None:
    """Regression for Run-2: stray `import discord` before the spec."""
    (Path(ctx.work_dir) / "requirements.txt").write_text(
        "import discord\ndiscord.py>=2.3\n", encoding="utf-8"
    )
    result = await _make_check_requirements(ctx)({})
    assert result.startswith("REQUIREMENTS INVALID")
    assert "line 1" in result


async def test_check_requirements_accepts_options_lines(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "requirements.txt").write_text(
        "-r base.txt\n--extra-index-url https://example.com\ndiscord.py>=2.3\n",
        encoding="utf-8",
    )
    result = await _make_check_requirements(ctx)({})
    assert result.startswith("OK")


async def test_check_requirements_missing_file(ctx: ToolContext) -> None:
    result = await _make_check_requirements(ctx)({})
    assert result.startswith("ERROR: file not found")
