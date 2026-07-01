"""Fail-closed audit (Checkpoint-1 follow-up).

Every postcondition that reads file *content* and checks a substring / shape
must return ``False`` when the target file is missing — never pass vacuously
on an absent or empty artifact, which would inflate postcondition pass-rate
(an axis-1 signal). These tests lock that behaviour for every content-reading
predicate in the registry and in runtime_postconditions so a future refactor
can't silently regress one to fail-open.
"""

from __future__ import annotations

import pytest

from agora.fleet.runtime_postconditions import (
    postcond_bot_calls_tree_sync,
    postcond_no_code_after_main_block,
    postcond_python_imports,
    postcond_readme_only_references_existing_commands,
    postcond_requirements_parse,
)
from agora.plan.predicate_registry import build_predicate

# --------------------------------------------------------------- registry predicates

# (registry name, args) for every content-reading predicate. Each must fail
# closed when work_dir contains none of the referenced files.
_REGISTRY_CONTENT_PREDICATES = [
    ("file_contains", {"rel": "missing.md", "substring": "bot.py"}),
    ("py_compiles", {"rel": "missing.py"}),
    ("max_line_length", {"rel": "missing.md"}),
    ("tests_have_assertions", {"rel": "tests/missing_test.py"}),
    ("api_spec_is_valid", {"rel": "plan/missing_spec.md"}),
    ("class_attributes_consistent", {"rel": "missing.py"}),
    ("no_task_exceeds_complexity", {"plan_path": "missing.yaml"}),
    (
        "api_spec_covers_brief_deliverables",
        {"rel": "plan/missing_spec.md", "brief_rel": "plan/missing_brief.md"},
    ),
]


@pytest.mark.parametrize("name,args", _REGISTRY_CONTENT_PREDICATES)
def test_registry_content_predicate_fails_closed_on_missing_file(
    tmp_path, name, args
) -> None:
    pred = build_predicate(name, args)
    ctx = {"work_dir": str(tmp_path), "artifacts": [], "completions": []}
    passed, _reason = pred.evaluate(ctx)
    assert passed is False, f"{name} should fail closed on a missing file"


# --------------------------------------------------------------- runtime_postconditions

_RUNTIME_CONTENT_PREDICATES = [
    postcond_python_imports("missing.py"),
    postcond_requirements_parse("missing.txt"),
    postcond_bot_calls_tree_sync("missing.py"),
    postcond_readme_only_references_existing_commands("missing_README.md", "missing_bot.py"),
    postcond_no_code_after_main_block("missing.py"),
]


@pytest.mark.parametrize("pred", _RUNTIME_CONTENT_PREDICATES, ids=lambda p: p.name)
def test_runtime_content_predicate_fails_closed_on_missing_file(tmp_path, pred) -> None:
    passed, _reason = pred.evaluate({"work_dir": str(tmp_path)})
    assert passed is False, f"{pred.name} should fail closed on a missing file"


def test_pytest_passes_fails_closed_on_missing_target(tmp_path) -> None:
    """pytest_passes with a concrete (non-'.') rel must fail when it's absent."""
    pred = build_predicate("pytest_passes", {"rel": "missing_tests_dir"})
    passed, _reason = pred.evaluate({"work_dir": str(tmp_path)})
    assert passed is False


# --------------------------------------------------------------- empty-file case

def test_substring_presence_predicates_fail_on_empty_file(tmp_path) -> None:
    """A file that EXISTS but is empty must not pass a substring-presence check
    (the Checkpoint-1 concern: vacuous-true on empty content)."""
    (tmp_path / "empty.md").write_text("", encoding="utf-8")
    pred = build_predicate("file_contains", {"rel": "empty.md", "substring": "bot.py"})
    passed, _reason = pred.evaluate({"work_dir": str(tmp_path), "artifacts": []})
    assert passed is False

    (tmp_path / "empty.py").write_text("", encoding="utf-8")
    bot = postcond_bot_calls_tree_sync("empty.py")
    passed, _reason = bot.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
