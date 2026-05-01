"""Unit tests for the v2.5 scope-bounded error router."""

from __future__ import annotations

from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import TaskStatus
from agora.fleet.error_router import extract_failing_paths, find_owning_task

# ============================================================ extract_failing_paths


def test_extract_module_not_found_error():
    reason = (
        "pytest failed for tests/: exit 1\n"
        "E   ModuleNotFoundError: No module named 'src.url_shortener'"
    )
    paths = extract_failing_paths(reason)
    assert "src/url_shortener.py" in paths
    assert "src/url_shortener/__init__.py" in paths


def test_extract_cannot_import_name_from():
    reason = (
        "tests/test_x.py:1: in <module>\n"
        "    from src.cli import missing_thing\n"
        "E   ImportError: cannot import name 'missing_thing' from 'src.cli'"
    )
    paths = extract_failing_paths(reason)
    assert "src/cli.py" in paths


def test_extract_excludes_test_module_import_banner():
    """pytest's ``ImportError while importing test module '...test_cli.py'``
    banner is the tester's OWN file — must not be routed upstream."""
    reason = (
        "ImportError while importing test module 'C:/path/tests/test_cli_module.py'.\n"
        "tests/test_cli_module.py:4: in <module>\n"
        "    from src.domain import HashGenerator\n"
        "E   ModuleNotFoundError: No module named 'src.url_shortener'"
    )
    paths = extract_failing_paths(reason)
    # Upstream is extracted, test module is not.
    assert "src/url_shortener.py" in paths
    assert not any(p.endswith("test_cli_module.py") for p in paths)


def test_extract_handles_empty_input():
    assert extract_failing_paths("") == []
    assert extract_failing_paths("no errors here") == []


def test_extract_dedups_same_module():
    reason = (
        "E   ModuleNotFoundError: No module named 'src.foo'\n"
        "E   ModuleNotFoundError: No module named 'src.foo'"
    )
    paths = extract_failing_paths(reason)
    # Once each for .py + /__init__.py — no further duplication.
    assert paths.count("src/foo.py") == 1
    assert paths.count("src/foo/__init__.py") == 1


def test_extract_handles_nested_dotted_paths():
    reason = "E   ModuleNotFoundError: No module named 'pkg.sub.deep'"
    paths = extract_failing_paths(reason)
    assert "pkg/sub/deep.py" in paths
    assert "pkg/sub/deep/__init__.py" in paths


def test_extract_captures_multiple_distinct_errors():
    reason = (
        "E   ModuleNotFoundError: No module named 'src.a'\n"
        "...\n"
        "E   ModuleNotFoundError: No module named 'src.b'"
    )
    paths = extract_failing_paths(reason)
    assert "src/a.py" in paths
    assert "src/b.py" in paths


# =============================================================== find_owning_task


def _task(
    id_: str,
    output_path: str = "",
    postcondition_names: tuple[str, ...] = (),
) -> Task:
    return Task(
        id=id_,
        spec=Specification(
            postconditions=tuple(
                make_predicate(name, "", lambda _c: (True, ""))
                for name in postcondition_names
            ),
            description="",
        ),
        description="",
        agent_id="anyone",
        output_path=output_path,
        status=TaskStatus.PENDING,
    )


def test_find_owning_task_by_output_path():
    tasks = [
        _task("setup", output_path="src/__init__.py"),
        _task("core", output_path="src/url_shortener.py"),
    ]
    owner = find_owning_task("src/url_shortener.py", tasks)
    assert owner is not None
    assert owner.id == "core"


def test_find_owning_task_by_file_exists_postcondition_name():
    """When a task declares ``file_exists(rel=src/foo.py)`` as a postcondition,
    the registered predicate is named ``artifact_contains_src_foo.py``; the
    router reverse-maps."""
    tasks = [
        _task(
            "build",
            postcondition_names=("artifact_contains_src_foo.py",),
        ),
    ]
    owner = find_owning_task("src/foo.py", tasks)
    assert owner is not None
    assert owner.id == "build"


def test_find_owning_task_returns_none_when_unmatched():
    tasks = [_task("other", output_path="src/other.py")]
    assert find_owning_task("src/ghost.py", tasks) is None


def test_find_owning_task_empty_inputs():
    assert find_owning_task("", []) is None
    assert find_owning_task("src/x.py", []) is None
    assert find_owning_task("", [_task("x")]) is None


def test_find_owning_task_first_match_wins_on_tie():
    """Two tasks both claim the same file — the earlier-declared one wins
    (natural task-graph order)."""
    tasks = [
        _task("first", output_path="src/shared.py"),
        _task("second", output_path="src/shared.py"),
    ]
    owner = find_owning_task("src/shared.py", tasks)
    assert owner is not None
    assert owner.id == "first"
