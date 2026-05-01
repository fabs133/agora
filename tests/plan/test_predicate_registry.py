"""Predicate registry: factory round-trip + naming stability.

Key invariant: predicate names produced by the registered factories match
byte-for-byte the names the inline helpers in ``scripts/run_fastapi_crud_test.py``
produce. That equality is what lets a plan YAML + the Python runner share
:attr:`Specification.fingerprint` values for the loader round-trip test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.core.errors import AgoraError
from agora.plan.predicate_registry import (
    build_predicate,
    list_registered_predicates,
    postcond_file_contains,
    postcond_file_exists,
    postcond_mark_complete,
    postcond_py_compiles,
    register_predicate,
)

# Seed names we promise to keep registered — if any of these ever disappears
# every existing plan YAML breaks.
REQUIRED_NAMES = frozenset(
    {
        "file_exists",
        "file_contains",
        "mark_complete",
        "py_compiles",
        "python_imports",
        "pytest_passes",
        "requirements_parse",
        "bot_calls_tree_sync",
        "readme_commands_exist",
        "no_code_after_main_block",
        "no_task_exceeds_complexity",
        "tests_have_assertions",
    }
)


def test_required_names_are_all_registered():
    registered = set(list_registered_predicates())
    missing = REQUIRED_NAMES - registered
    assert not missing, f"registry missing expected names: {missing}"


def test_unknown_name_raises():
    with pytest.raises(AgoraError, match="unknown predicate"):
        build_predicate("not_a_real_predicate", {"rel": "x"})


def test_duplicate_registration_raises(registry_snapshot):
    with pytest.raises(AgoraError, match="already registered"):

        @register_predicate("file_exists")  # type: ignore[misc]
        def _dup(rel: str):
            return postcond_file_exists(rel)


def test_file_exists_naming_matches_inline_runner():
    p = postcond_file_exists("kb/intro.md")
    # Matches run_fastapi_crud_test.py:86 pattern exactly: ``/``→``_`` only,
    # no [:60] truncation, no ``.``→``_``.
    assert p.name == "artifact_contains_kb_intro.md"


def test_file_contains_naming_matches_inline_runner():
    p = postcond_file_contains("app.py", "FastAPI")
    assert p.name == "app.py_has_FastAPI"


def test_file_contains_naming_substring_sanitized():
    p = postcond_file_contains("README.md", "DISCORD TOKEN.val")
    # Spaces → underscores, dots → underscores in the substring portion.
    assert p.name == "README.md_has_DISCORD_TOKEN_val"


def test_mark_complete_name_is_stable():
    p = postcond_mark_complete()
    assert p.name == "mark_complete_called"


def test_py_compiles_naming_matches_fastapi_pattern():
    # run_fastapi_crud_test.py:143 uses ``/``→``_`` only (not ``.``→``_``).
    p = postcond_py_compiles("app.py")
    assert p.name == "app.py_py_compiles"


def test_build_predicate_file_exists_round_trip():
    direct = postcond_file_exists("kb/intro.md")
    via_registry = build_predicate("file_exists", {"rel": "kb/intro.md"})
    assert direct.name == via_registry.name
    assert direct.description == via_registry.description


def test_py_compiles_passes_on_valid_module(tmp_path: Path):
    """The lifted py_compiles factory invokes py_compile + AST checks."""
    (tmp_path / "m.py").write_text(
        "import os\nPATH = os.environ.get('X', 'default')\n", encoding="utf-8"
    )
    p = postcond_py_compiles("m.py")
    passed, _ = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is True


def test_py_compiles_fails_on_syntax_error(tmp_path: Path):
    (tmp_path / "bad.py").write_text("def foo(\n", encoding="utf-8")
    p = postcond_py_compiles("bad.py")
    passed, reason = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "SyntaxError" in reason or "py_compile" in reason


def test_py_compiles_fails_on_undefined_name(tmp_path: Path):
    (tmp_path / "undef.py").write_text(
        "VALUE = os.environ['HOME']\n", encoding="utf-8"  # missing import os
    )
    p = postcond_py_compiles("undef.py")
    passed, reason = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "undefined" in reason.lower() or "name" in reason.lower()


def test_py_compiles_fails_on_missing_file(tmp_path: Path):
    p = postcond_py_compiles("nope.py")
    passed, reason = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "does not exist" in reason


def test_py_compiles_requires_work_dir():
    p = postcond_py_compiles("any.py")
    passed, reason = p.evaluate({})
    assert passed is False
    assert "work_dir" in reason


def test_file_contains_requires_work_dir():
    p = postcond_file_contains("any.md", "x")
    passed, reason = p.evaluate({})
    assert passed is False
    assert "work_dir" in reason


def test_file_contains_missing_file(tmp_path: Path):
    p = postcond_file_contains("ghost.md", "x")
    passed, reason = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "does not exist" in reason


def test_build_predicate_runtime_evaluation_still_works(tmp_path: Path):
    # Smoke-test that the evaluate callable survives the registry round-trip.
    # Note: the inline runner pattern (and therefore the registered factory)
    # returns a static reason string that only becomes meaningful when
    # passed is False — matches the existing convention.
    p = build_predicate("file_contains", {"rel": "note.md", "substring": "hello"})
    (tmp_path / "note.md").write_text("say hello world", encoding="utf-8")
    ctx = {"work_dir": str(tmp_path), "artifacts": []}
    passed, _ = p.evaluate(ctx)
    assert passed is True

    (tmp_path / "note.md").write_text("goodbye world", encoding="utf-8")
    passed, reason = p.evaluate(ctx)
    assert passed is False
    assert "hello" in reason
