"""Scope-bounded error routing — v2.5.

When a test task fails because an *upstream* task's output is broken (e.g.
``implement_tests`` fails with ``ModuleNotFoundError: No module named
'src.url_shortener'`` pointing at a file owned by ``implement_core_module``),
the framework should return the failure to the owning task instead of letting
the tester keep cycling on something it can't fix.

This module is the pure-helper layer that the orchestrator composes with the
retry machinery. Two functions, no I/O:

- :func:`extract_failing_paths` — parse a pytest / import failure string and
  return workspace-relative paths referenced by ``ImportError`` /
  ``ModuleNotFoundError`` entries. Test modules (the file pytest was trying to
  *load*) are excluded because those are owned by the tester, not an upstream.
- :func:`find_owning_task` — given a file path and the project's task list,
  return the task whose output is that file. Matches against both
  ``task.output_path`` and ``file_exists(rel=...)`` postconditions.

The orchestrator wires them together: on a failed ``pytest_passes``
postcondition, extract the broken paths, look up the owning task, flip it
back to PENDING with structural feedback in its next system prompt.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from agora.core.task import Task

#: ``No module named 'src.url_shortener'`` / ``No module named "src.url_shortener"``
#: (captures the dotted module path)
_MODULE_NOT_FOUND_RE = re.compile(
    r"No module named ['\"]([\w.]+)['\"]"
)

#: ``cannot import name 'X' from 'src.cli'`` / ``from partially initialized module 'src.cli'``
_CANNOT_IMPORT_FROM_RE = re.compile(
    r"(?:cannot import name ['\"][^'\"]+['\"] from "
    r"(?:partially initialized module )?['\"])([\w.]+)(?:['\"])"
)

#: Pytest's collection banner: ``ImportError while importing test module '<abs path>'``
#: We detect + EXCLUDE those paths — they're the tester's own output and
#: wouldn't be routed upstream.
_TEST_MODULE_IMPORT_RE = re.compile(
    r"[Ii]mport[Ee]rror while importing test module ['\"]([^'\"]+)['\"]"
)


def _dotted_to_relpath(dotted: str) -> str:
    """Turn ``src.url_shortener`` → ``src/url_shortener.py``. Used for the
    ``ModuleNotFoundError`` / ``ImportError from X`` cases where pytest names
    the module in dotted form. We emit the leaf ``.py`` path; if the real
    on-disk layout is actually a package (``<dotted>/__init__.py``), the
    owning-task lookup handles both by checking both candidates."""
    if not dotted:
        return ""
    parts = dotted.split(".")
    return "/".join(parts) + ".py"


def _dotted_to_package_init(dotted: str) -> str:
    """Alternate path: ``src.url_shortener`` → ``src/url_shortener/__init__.py``.
    Emitted alongside the module form so lookups can match either layout."""
    if not dotted:
        return ""
    return "/".join(dotted.split(".")) + "/__init__.py"


def extract_failing_paths(failure_reason: str) -> list[str]:
    """Parse a pytest failure string; return workspace-relative paths of
    modules referenced by ``ImportError`` / ``ModuleNotFoundError`` in the
    traceback.

    Returns an ordered, de-duplicated list. Empty list means: no routable
    import error was found in this reason string, caller falls back to the
    normal failure path.

    The returned paths include both ``foo/bar.py`` and ``foo/bar/__init__.py``
    candidates for each dotted module so the owning-task lookup can match
    whichever form the project actually uses.

    Test-module import failures (pytest couldn't load the test file itself —
    the tester's own problem) are excluded from the output.
    """
    if not failure_reason:
        return []

    # Track the tester's own test file paths so we can exclude them. pytest's
    # banner uses absolute paths; we compare by basename tail.
    excluded_tails: set[str] = set()
    for m in _TEST_MODULE_IMPORT_RE.finditer(failure_reason):
        abs_path = m.group(1).replace("\\", "/")
        # Use the last two segments as the tail signature (works for
        # ``tests/test_cli_module.py``).
        parts = abs_path.split("/")
        tail = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
        excluded_tails.add(tail)

    paths: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        candidate = candidate.replace("\\", "/")
        if not candidate or candidate in seen:
            return
        if any(candidate.endswith(t) for t in excluded_tails):
            return
        seen.add(candidate)
        paths.append(candidate)

    # ModuleNotFoundError matches first (most common).
    for m in _MODULE_NOT_FOUND_RE.finditer(failure_reason):
        dotted = m.group(1)
        _add(_dotted_to_relpath(dotted))
        _add(_dotted_to_package_init(dotted))

    # ImportError: cannot import name 'X' from 'Y'.
    for m in _CANNOT_IMPORT_FROM_RE.finditer(failure_reason):
        dotted = m.group(1)
        _add(_dotted_to_relpath(dotted))
        _add(_dotted_to_package_init(dotted))

    return paths


def find_owning_task(file_path: str, tasks: Sequence[Task]) -> Task | None:
    """Return the task whose output claims ``file_path``, or None.

    Matching rules (in order — the first hit wins):
      1. ``task.output_path`` equals ``file_path`` (normalized slashes).
      2. Any postcondition in ``task.spec.postconditions`` whose name encodes
         the same relative path (e.g. the predicate factory
         ``file_exists(rel="src/foo.py")`` registers a predicate named
         ``artifact_contains_src_foo.py`` — we re-derive the rel from the
         name for matching).

    Tasks in ``tasks`` are walked in their natural list order, which mirrors
    the plan's declared task order. Self-owned lookups (a task routing
    to itself) are the caller's responsibility to filter.
    """
    if not file_path or not tasks:
        return None
    norm = file_path.replace("\\", "/")
    # Derive the rel-form of the predicate name. ``file_exists(rel="src/foo.py")``
    # registers via ``_require(f"artifact_contains_{rel.replace('/', '_')}")``,
    # yielding e.g. ``artifact_contains_src_foo.py``.
    expected_artifact_name = "artifact_contains_" + norm.replace("/", "_")
    # ``py_compiles`` uses ``{rel.replace('/', '_')}_py_compiles[:60]`` — we
    # don't match that one; it's an owner's self-check, not a file claim.

    for task in tasks:
        op = (task.output_path or "").replace("\\", "/")
        if op == norm:
            return task
        for pred in task.spec.postconditions:
            if pred.name == expected_artifact_name:
                return task
    return None


__all__ = ["extract_failing_paths", "find_owning_task"]
