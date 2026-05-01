"""Tests for the subprocess-based postcondition helpers."""

from __future__ import annotations

from pathlib import Path

from agora.fleet.runtime_postconditions import (
    postcond_bot_calls_tree_sync,
    postcond_pytest_passes,
    postcond_python_imports,
    postcond_readme_only_references_existing_commands,
    postcond_requirements_parse,
)


def _ctx(work_dir: Path) -> dict:
    return {"work_dir": str(work_dir)}


# ------------------------------------------------------------ python_imports


def test_postcond_python_imports_passes_for_valid_module(tmp_path: Path) -> None:
    (tmp_path / "good.py").write_text("import math\nX = math.pi\n", encoding="utf-8")
    ok, msg = postcond_python_imports("good.py").evaluate(_ctx(tmp_path))
    assert ok is True, msg


def test_postcond_python_imports_flags_attributeerror(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "class _M: pass\nm = _M()\nX = m.random.randint(1, 6)\n",
        encoding="utf-8",
    )
    ok, msg = postcond_python_imports("bad.py").evaluate(_ctx(tmp_path))
    assert ok is False
    assert "AttributeError" in msg


def test_postcond_python_imports_flags_missing_file(tmp_path: Path) -> None:
    ok, msg = postcond_python_imports("ghost.py").evaluate(_ctx(tmp_path))
    assert ok is False
    assert "does not exist" in msg


# --------------------------------------------------------------- pytest_passes


def test_postcond_pytest_passes_for_green_suite(tmp_path: Path) -> None:
    (tmp_path / "test_green.py").write_text(
        "def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    ok, msg = postcond_pytest_passes("test_green.py").evaluate(_ctx(tmp_path))
    assert ok is True, msg


def test_postcond_pytest_fails_on_red_suite(tmp_path: Path) -> None:
    (tmp_path / "test_red.py").write_text(
        "def test_no():\n    assert False\n", encoding="utf-8"
    )
    ok, msg = postcond_pytest_passes("test_red.py").evaluate(_ctx(tmp_path))
    assert ok is False
    assert "pytest failed" in msg


def test_postcond_pytest_missing_file(tmp_path: Path) -> None:
    ok, msg = postcond_pytest_passes("nope.py").evaluate(_ctx(tmp_path))
    assert ok is False
    assert "does not exist" in msg


# ----------------------------------------------------------- requirements_parse


def test_postcond_requirements_parse_accepts_pinned_spec(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "discord.py>=2.3\nrequests\n", encoding="utf-8"
    )
    ok, msg = postcond_requirements_parse().evaluate(_ctx(tmp_path))
    assert ok is True, msg


def test_postcond_requirements_parse_rejects_import_line(tmp_path: Path) -> None:
    """Regression for Run-2: `import discord` as the first line."""
    (tmp_path / "requirements.txt").write_text(
        "import discord\ndiscord.py>=2.3\n", encoding="utf-8"
    )
    ok, msg = postcond_requirements_parse().evaluate(_ctx(tmp_path))
    assert ok is False
    assert "line 1" in msg


def test_postcond_requirements_parse_missing_file(tmp_path: Path) -> None:
    ok, msg = postcond_requirements_parse().evaluate(_ctx(tmp_path))
    assert ok is False


# --------------------------------------------------------- bot_calls_tree_sync


def test_postcond_bot_calls_tree_sync_present(tmp_path: Path) -> None:
    (tmp_path / "bot.py").write_text(
        "async def on_ready():\n    await bot.tree.sync()\n", encoding="utf-8"
    )
    ok, msg = postcond_bot_calls_tree_sync().evaluate(_ctx(tmp_path))
    assert ok is True, msg


def test_postcond_bot_calls_tree_sync_missing(tmp_path: Path) -> None:
    (tmp_path / "bot.py").write_text(
        "async def on_ready():\n    print('ready')\n", encoding="utf-8"
    )
    ok, msg = postcond_bot_calls_tree_sync().evaluate(_ctx(tmp_path))
    assert ok is False
    assert "tree.sync" in msg


# --------------------------------- readme_only_references_existing_commands


def test_postcond_readme_accepts_matching_commands(tmp_path: Path) -> None:
    (tmp_path / "bot.py").write_text(
        "@bot.tree.command(name='ping')\n"
        "async def ping(i): pass\n"
        "@bot.tree.command(name='roll')\n"
        "async def roll(i): pass\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "# bot\nUse /ping to ping, /roll to roll.\n", encoding="utf-8"
    )
    ok, msg = postcond_readme_only_references_existing_commands().evaluate(_ctx(tmp_path))
    assert ok is True, msg


def test_postcond_readme_flags_hallucinated_help(tmp_path: Path) -> None:
    """Regression for Run-2: README advertised /help that was never implemented."""
    (tmp_path / "bot.py").write_text(
        "@bot.tree.command(name='ping')\nasync def ping(i): pass\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "Commands: /ping, /help, /roll\n", encoding="utf-8"
    )
    ok, msg = postcond_readme_only_references_existing_commands().evaluate(_ctx(tmp_path))
    assert ok is False
    assert "help" in msg
    assert "roll" in msg


def test_postcond_readme_ignores_shebang_tokens(tmp_path: Path) -> None:
    (tmp_path / "bot.py").write_text(
        "@bot.tree.command(name='ping')\nasync def ping(i): pass\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "#!/usr/bin/env python\nRun `python bot.py` then /ping.\n",
        encoding="utf-8",
    )
    ok, msg = postcond_readme_only_references_existing_commands().evaluate(_ctx(tmp_path))
    assert ok is True, msg


def test_postcond_readme_missing_bot(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Use /ping\n", encoding="utf-8")
    ok, msg = postcond_readme_only_references_existing_commands().evaluate(_ctx(tmp_path))
    assert ok is False
