"""Tests for the post-``if __name__ == '__main__':`` unreachable-code detector.

Regression net for Run 13's bug: ``build_roll`` inserted a ``@bot.tree.command``
handler AFTER ``bot.run(TOKEN)``. Tests (import time, ``__name__='bot'``) saw
the handler registered; production (``__name__='__main__'``, ``bot.run`` blocks)
never registered it. This detector catches the exact shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.core.types import AgentRole
from agora.fleet.inner_tools import (
    ToolContext,
    _find_code_after_main_block,
    get_tool_executor,
)
from agora.fleet.runtime_postconditions import postcond_no_code_after_main_block


# ============================================================================
# _find_code_after_main_block — AST-level detector
# ============================================================================


def test_detector_returns_empty_when_no_main_block() -> None:
    src = "import os\nx = 1\n"
    assert _find_code_after_main_block(src) == []


def test_detector_returns_empty_when_main_block_is_last() -> None:
    src = (
        "import os\n"
        "def f(): pass\n"
        "if __name__ == '__main__':\n"
        "    f()\n"
    )
    assert _find_code_after_main_block(src) == []


def test_detector_flags_import_after_main_block() -> None:
    src = (
        "import os\n"
        "if __name__ == '__main__':\n"
        "    os.environ['X']\n"
        "import random\n"
    )
    stragglers = _find_code_after_main_block(src)
    assert len(stragglers) == 1
    kind, lineno = stragglers[0]
    assert "import" in kind
    assert lineno == 4


def test_detector_flags_function_def_after_main_block() -> None:
    """The exact Run-13 bug."""
    src = (
        "import discord\n"
        "from discord.ext import commands\n"
        "bot = commands.Bot(command_prefix='!')\n"
        "if __name__ == '__main__':\n"
        "    bot.run('TOKEN')\n"
        "import random\n"
        "\n"
        "@bot.tree.command(name='roll')\n"
        "async def roll(i, sides=6):\n"
        "    pass\n"
    )
    stragglers = _find_code_after_main_block(src)
    assert len(stragglers) == 2
    kinds = [k for k, _ in stragglers]
    assert any("import" in k for k in kinds)
    assert any("roll" in k for k in kinds)


def test_detector_flags_all_trailing_statements() -> None:
    src = (
        "if __name__ == '__main__':\n"
        "    pass\n"
        "x = 1\n"
        "y = 2\n"
        "def f(): pass\n"
        "class C: pass\n"
    )
    stragglers = _find_code_after_main_block(src)
    assert len(stragglers) == 4


def test_detector_accepts_reversed_comparison() -> None:
    """``'__main__' == __name__`` is semantically identical to the idiomatic form."""
    src = (
        "if '__main__' == __name__:\n"
        "    pass\n"
        "x = 1\n"
    )
    assert len(_find_code_after_main_block(src)) == 1


def test_detector_ignores_function_level_main_check() -> None:
    """A ``if __name__ == '__main__':`` INSIDE a function isn't the module-level one."""
    src = (
        "def main():\n"
        "    if __name__ == '__main__':\n"
        "        pass\n"
        "    y = 1\n"
        "print('module level')\n"
    )
    assert _find_code_after_main_block(src) == []


def test_detector_handles_syntax_error_gracefully() -> None:
    """Broken files return empty — py_compile catches those first."""
    src = "def broken(\n"
    assert _find_code_after_main_block(src) == []


# ============================================================================
# check_python tool — reports after-__main__ code
# ============================================================================


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


async def test_check_python_reports_code_after_main_block(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "bot.py").write_text(
        "import discord\n"
        "from discord.ext import commands\n"
        "bot = commands.Bot(command_prefix='!')\n"
        "if __name__ == '__main__':\n"
        "    bot.run('TOKEN')\n"
        "import random\n"
        "\n"
        "@bot.tree.command(name='roll')\n"
        "async def roll(i, sides=6):\n"
        "    pass\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["check_python"]({"path": "bot.py"})
    assert result.startswith("unreachable code")
    assert "bot.py" in result
    assert "roll" in result or "import" in result


async def test_check_python_still_returns_ok_when_main_is_last(ctx: ToolContext) -> None:
    (Path(ctx.work_dir) / "good.py").write_text(
        "import os\n"
        "def go(): print(os.getcwd())\n"
        "if __name__ == '__main__':\n"
        "    go()\n",
        encoding="utf-8",
    )
    executor = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    result = await executor["check_python"]({"path": "good.py"})
    assert result.startswith("OK")


# ============================================================================
# postcond_no_code_after_main_block
# ============================================================================


def _ctx(work_dir: Path) -> dict:
    return {"work_dir": str(work_dir)}


def test_postcond_passes_on_clean_main_block(tmp_path: Path) -> None:
    (tmp_path / "bot.py").write_text(
        "if __name__ == '__main__':\n    pass\n", encoding="utf-8"
    )
    ok, msg = postcond_no_code_after_main_block("bot.py").evaluate(_ctx(tmp_path))
    assert ok is True, msg


def test_postcond_fails_on_run13_shape(tmp_path: Path) -> None:
    (tmp_path / "bot.py").write_text(
        "import discord\n"
        "bot = None\n"
        "if __name__ == '__main__':\n"
        "    bot.run('t')\n"
        "import random\n"
        "@bot.tree.command(name='roll')\n"
        "async def roll(i, sides=6):\n"
        "    pass\n",
        encoding="utf-8",
    )
    ok, msg = postcond_no_code_after_main_block("bot.py").evaluate(_ctx(tmp_path))
    assert ok is False
    assert "unreachable" in msg.lower() or "after `if __name__" in msg
    assert "roll" in msg or "import" in msg


def test_postcond_fails_on_missing_file(tmp_path: Path) -> None:
    ok, msg = postcond_no_code_after_main_block("nope.py").evaluate(_ctx(tmp_path))
    assert ok is False
    assert "does not exist" in msg


def test_postcond_passes_when_no_main_block_at_all(tmp_path: Path) -> None:
    """A module without any __main__ block is fine — nothing to be after."""
    (tmp_path / "lib.py").write_text("x = 1\ndef f(): pass\n", encoding="utf-8")
    ok, msg = postcond_no_code_after_main_block("lib.py").evaluate(_ctx(tmp_path))
    assert ok is True, msg
