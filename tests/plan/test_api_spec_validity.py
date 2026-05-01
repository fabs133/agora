"""Tests for Sprint 7.5(d) api_spec_is_valid predicate."""

from __future__ import annotations

from pathlib import Path

from agora.plan.predicate_registry import (
    build_predicate,
    postcond_api_spec_is_valid,
)


def _eval(tmp_path: Path, spec_text: str) -> tuple[bool, str]:
    (tmp_path / "plan").mkdir(exist_ok=True)
    (tmp_path / "plan" / "api_spec.md").write_text(spec_text, encoding="utf-8")
    pred = postcond_api_spec_is_valid("plan/api_spec.md")
    return pred.evaluate({"work_dir": str(tmp_path)})


def test_valid_spec_with_class_passes(tmp_path: Path):
    spec = (
        "## module: src/cli.py\n\n"
        "class URLShortener:\n"
        "    def __init__(self) -> None: ...\n"
        "    def shorten(self, url: str) -> str: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is True, reason


def test_valid_spec_with_functions_only_passes(tmp_path: Path):
    spec = (
        "## module: src/util.py\n\n"
        "def helper(x: int) -> int: ...\n"
        "def other(s: str) -> bool: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is True, reason


def test_spec_with_toplevel_self_rejected(tmp_path: Path):
    """Regression guard for the observed 7B failure: top-level functions
    with spurious `self` parameter."""
    spec = (
        "## module: src/cli.py\n\n"
        "def add_url(self, long_url: str) -> str: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    assert "add_url" in reason
    assert "self" in reason


def test_spec_with_empty_modules_rejected(tmp_path: Path):
    """Module header with no body is surfaced as "empty module body" via
    the C5 per-section parse-status check."""
    spec = "## module: src/x.py\n\n"
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    # v2.9(C5): the new per-section check flags the empty body explicitly.
    assert "empty" in reason.lower()
    assert "src/x.py" in reason


def test_spec_without_module_headers_rejected(tmp_path: Path):
    spec = "Some plain markdown, no module sections.\n"
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    # v2.9(C5): message was rephrased for clarity — still rejects the
    # no-module-headers case.
    assert "no `## module:" in reason


def test_spec_missing_file_rejected(tmp_path: Path):
    pred = postcond_api_spec_is_valid("plan/api_spec.md")
    passed, reason = pred.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "does not exist" in reason


def test_spec_mixed_valid_class_with_bad_function(tmp_path: Path):
    """A class + a rogue top-level self-function: still rejected because
    of the rogue function."""
    spec = (
        "## module: src/cli.py\n\n"
        "class OK:\n"
        "    def method(self) -> None: ...\n"
        "\n"
        "def bad(self, x: int) -> int: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    assert "bad" in reason


def test_spec_with_classmethod_is_fine(tmp_path: Path):
    """Inside a class, `self` methods are perfectly fine."""
    spec = (
        "## module: src/a.py\n\n"
        "class Thing:\n"
        "    def __init__(self) -> None: ...\n"
        "    def do(self, x: int) -> int: ...\n"
    )
    passed, _ = _eval(tmp_path, spec)
    assert passed is True


def test_spec_with_test_module_rejected(tmp_path: Path):
    """The api_spec is for PRODUCTION code only — test modules belong in
    the test-authoring pipeline, not here."""
    spec = (
        "## module: src/cli.py\n\n"
        "class OK:\n    def run(self) -> None: ...\n\n"
        "## module: src/tests/test_shortener.py\n\n"
        "def test_add() -> None: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    assert "test" in reason.lower()


def test_spec_with_tests_prefix_rejected(tmp_path: Path):
    spec = (
        "## module: tests/test_cli.py\n\n"
        "def test_x() -> None: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False


def test_spec_with_non_src_module_rejected(tmp_path: Path):
    spec = "## module: scripts/cli.py\n\ndef main() -> None: ...\n"
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    assert "src" in reason.lower()


def test_spec_via_registry(tmp_path: Path):
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "api_spec.md").write_text(
        "## module: src/x.py\n\ndef f() -> None: ...\n", encoding="utf-8"
    )
    pred = build_predicate("api_spec_is_valid", {"rel": "plan/api_spec.md"})
    passed, _ = pred.evaluate({"work_dir": str(tmp_path)})
    assert passed is True


# =================================================================
# v2.9 / C5 — reject structurally-broken specs that parse_api_spec
# would silently drop sections from. Regression guard for the
# 2026-04-22 failure where the plan-builder emitted three concatenated
# spec blocks and the executor only saw the first one.
# =================================================================


def test_spec_with_duplicate_module_paths_rejected(tmp_path: Path):
    """Same `## module:` path declared more than once → reject. Downstream
    seed_workspace's first-wins policy would silently drop subsequent
    sections; catching it at authoring time forces the architect to
    re-author one coherent section."""
    spec = (
        "## module: src/url_shortener.py\n\n"
        "class URLShortener:\n"
        "    def __init__(self) -> None: ...\n"
        "    def add_url(self, url: str) -> str: ...\n\n"
        "## module: src/url_shortener.py\n\n"
        "class URLShortener:\n"
        "    def __init__(self, storage: type) -> None: ...\n"
        "    def add_url(self, url: str) -> str: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    # Error surfaces the duplicate path + both line numbers.
    assert "duplicate" in reason.lower()
    assert "src/url_shortener.py" in reason


def test_spec_with_unparseable_module_body_rejected(tmp_path: Path):
    """A module body mixing markdown prose with signatures fails
    ast.parse and is silently dropped by parse_api_spec; the validator
    must surface the dropped section instead of letting the spec look
    valid when entire modules are missing."""
    spec = (
        "## module: src/url_shortener.py\n\n"
        "class URLShortener:\n"
        "    def __init__(self) -> None: ...\n\n"
        "## module: src/storage.py\n\n"
        "def persist_to_disk(path: str) -> None: ...\n\n"
        "- `src/cli.py`: a bullet item that breaks ast.parse\n"
        "def load_from_disk(path: str) -> dict: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    assert "src/storage.py" in reason
    assert "parse" in reason.lower() or "syntax" in reason.lower()


def test_spec_with_placeholder_token_rejected(tmp_path: Path):
    """`<pkg>.<mod>` placeholder tokens the model sometimes copies from
    instructions produce SyntaxError — must surface, not silently drop."""
    spec = (
        "## module: src/app.py\n\n"
        "class Thing:\n"
        "    def method(self) -> <pkg>.Type: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    # Either the parse-error branch or the zero-symbol branch is fine.
    assert "src/app.py" in reason


def test_spec_with_markdown_bullets_rejected(tmp_path: Path):
    """Markdown bullets like `- src/cli.py` parse as valid Python (unary
    minus on a binary-op chain) but are clearly not signatures. v2.9
    follow-up check rejects non-signature top-level statements."""
    spec = (
        "## module: src/core.py\n\n"
        "class Core:\n"
        "    def do(self) -> None: ...\n\n"
        "- src/cli.py\n"
        "- src/tests.py\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    assert "non-signature" in reason.lower() or "expression" in reason.lower()
    assert "src/core.py" in reason


def test_spec_with_assignment_at_top_level_rejected(tmp_path: Path):
    """A stray `x = 1` at module top level is structural noise in an
    api_spec — rejected. Signatures only."""
    spec = (
        "## module: src/app.py\n\n"
        "class Thing:\n"
        "    def f(self) -> None: ...\n\n"
        "DEFAULT_PORT = 8080\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    assert "src/app.py" in reason


def test_spec_with_imports_still_accepted(tmp_path: Path):
    """Imports at module top are discouraged but allowed — some architects
    add them for typing reasons and we don't want to break that."""
    spec = (
        "## module: src/app.py\n\n"
        "import os\n"
        "from typing import Optional\n\n"
        "class Thing:\n"
        "    def f(self) -> Optional[str]: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is True, reason


def test_spec_with_docstring_still_accepted(tmp_path: Path):
    """Module-level docstring is a common Python pattern — allow it."""
    spec = (
        "## module: src/app.py\n\n"
        '"""The application module."""\n\n'
        "class Thing:\n"
        "    def f(self) -> None: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is True, reason


def test_spec_with_duplicate_class_in_same_module_rejected(tmp_path: Path):
    """Two `class X` blocks in ONE module body: ast.parse accepts but the
    stub scaffolder writes both, producing ambiguous edits downstream."""
    spec = (
        "## module: src/app.py\n\n"
        "class URLShortener:\n"
        "    def __init__(self) -> None: ...\n"
        "class URLShortener:\n"  # duplicate class name
        "    def __init__(self, storage: type) -> None: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    assert "URLShortener" in reason
    assert "more than once" in reason.lower() or "duplicate" in reason.lower()


def test_spec_with_duplicate_function_in_same_module_rejected(tmp_path: Path):
    spec = (
        "## module: src/app.py\n\n"
        "def helper(x: int) -> int: ...\n"
        "def helper(x: str) -> str: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    assert "helper" in reason


def test_spec_error_lists_multiple_problems(tmp_path: Path):
    """When the architect ships a truly mangled spec, the error surfaces
    several problems at once so the retry can fix them in ONE pass
    instead of playing whack-a-mole over N retries."""
    spec = (
        "## module: src/a.py\n\n"
        "class Thing:\n"
        "    def method(self) -> None: ...\n\n"
        "## module: src/a.py\n\n"  # duplicate path
        "class Thing:\n"
        "    def method(self) -> None: ...\n\n"
        "## module: src/b.py\n\n"
        "- markdown list item\n"  # syntax error
        "def f() -> None: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is False
    assert "duplicate" in reason.lower()
    assert "src/a.py" in reason
    assert "src/b.py" in reason


def test_spec_clean_single_module_still_passes(tmp_path: Path):
    """Regression guard: a clean spec with one module and a single class
    (the happy path the planner is supposed to produce) still passes."""
    spec = (
        "## module: src/url_shortener.py\n\n"
        "class URLShortener:\n"
        "    def __init__(self) -> None: ...\n"
        "    def add_url(self, long_url: str) -> str: ...\n"
        "    def lookup_url(self, short_hash: str) -> str: ...\n"
        "    def list_mappings(self) -> list: ...\n"
        "    def save(self, path: str) -> None: ...\n"
        "    def load(self, path: str) -> None: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is True, reason


def test_spec_multiple_distinct_modules_passes(tmp_path: Path):
    """Two DIFFERENT module paths — not duplicates — still pass."""
    spec = (
        "## module: src/core.py\n\n"
        "class URLShortener:\n"
        "    def add(self, url: str) -> str: ...\n\n"
        "## module: src/util.py\n\n"
        "def helper(x: int) -> int: ...\n"
    )
    passed, reason = _eval(tmp_path, spec)
    assert passed is True, reason
