"""Unit tests for the test-authoring scaffold helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.plan.test_scaffolder import (
    discover_modules,
    parse_deliverables,
    render_scaffold,
    replace_test_body,
    snake_from_deliverable,
)

# --------------------------------------------------------------- parse_deliverables


def test_parse_deliverables_extracts_bullets():
    brief = (
        "# Brief: URL shortener CLI\n\n"
        "## Key deliverables\n"
        "- Add a long URL and get a short 6-char hash back\n"
        "- Look up the original URL given the hash\n"
        "- List all saved mappings\n"
        "\n"
        "## Some other section\n"
        "- this should NOT be included\n"
    )
    assert parse_deliverables(brief) == [
        "Add a long URL and get a short 6-char hash back",
        "Look up the original URL given the hash",
        "List all saved mappings",
    ]


def test_parse_deliverables_case_insensitive_header():
    brief = "## KEY DELIVERABLES\n- foo\n- bar\n"
    assert parse_deliverables(brief) == ["foo", "bar"]


def test_parse_deliverables_missing_section_returns_empty():
    assert parse_deliverables("# Title\n\nNo deliverables here.") == []


def test_parse_deliverables_empty_input():
    assert parse_deliverables("") == []


def test_parse_deliverables_supports_star_bullets():
    brief = "## Key deliverables\n* one\n* two\n"
    assert parse_deliverables(brief) == ["one", "two"]


def test_parse_deliverables_dedupes_identical_bullets():
    """Plan-builder architect sometimes duplicates bullets when enriching;
    downstream duplicate pytest.skip strings break edit_file_replace."""
    brief = (
        "## Key deliverables\n"
        "- Add a URL\n"
        "- Look up a URL\n"
        "- Add a URL\n"  # exact duplicate
        "- add a URL.\n"  # case/punctuation variant of dupe
    )
    assert parse_deliverables(brief) == ["Add a URL", "Look up a URL"]


# ---------------------------------------------------------------- discover_modules


def test_discover_modules_returns_empty_without_src(tmp_path: Path):
    assert discover_modules(tmp_path) == {}


def test_discover_modules_finds_public_names_in_package_layout(tmp_path: Path):
    pkg = tmp_path / "src" / "url_shortener_mvp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "domain.py").write_text(
        "class URLShortener:\n    def shorten(self, url): pass\n\n"
        "def helper(): pass\n"
        "_private = 1\n",
        encoding="utf-8",
    )
    (pkg / "cli.py").write_text(
        "import argparse\ndef main(): pass\n",
        encoding="utf-8",
    )
    # Test file + __init__ should be skipped.
    (pkg / "test_something.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (pkg / "_private.py").write_text("def secret(): pass\n", encoding="utf-8")

    mods = discover_modules(tmp_path)
    assert "url_shortener_mvp.domain" in mods
    assert set(mods["url_shortener_mvp.domain"]) == {"URLShortener", "helper"}
    assert "url_shortener_mvp.cli" in mods
    assert mods["url_shortener_mvp.cli"] == ["main"]
    # Skipped files should not appear.
    assert "url_shortener_mvp.test_something" not in mods
    assert "url_shortener_mvp._private" not in mods


def test_discover_modules_handles_nested_subpackages(tmp_path: Path):
    pkg = tmp_path / "src" / "app"
    (pkg / "core").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core" / "domain.py").write_text(
        "class Thing: pass\n", encoding="utf-8"
    )
    mods = discover_modules(tmp_path)
    assert "app.core.domain" in mods


def test_discover_modules_skips_unparseable_files(tmp_path: Path):
    pkg = tmp_path / "src" / "broken_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "bad.py").write_text("def oops(:\n", encoding="utf-8")  # SyntaxError
    (pkg / "good.py").write_text("def ok(): pass\n", encoding="utf-8")
    mods = discover_modules(tmp_path)
    assert "broken_pkg.bad" not in mods
    assert "broken_pkg.good" in mods


# ------------------------------------------------------------ snake_from_deliverable


def test_snake_from_deliverable_basic():
    assert (
        snake_from_deliverable("Add a long URL and get a short 6-char hash back")
        .startswith("test_add_a_long_url")
    )


def test_snake_from_deliverable_empty_gets_fallback():
    assert snake_from_deliverable("") == "test_deliverable"


def test_snake_from_deliverable_truncates_long_names():
    long_bullet = "A" * 200
    result = snake_from_deliverable(long_bullet)
    assert len(result) <= 50


def test_snake_from_deliverable_already_prefixed():
    assert snake_from_deliverable("test already prefixed") == "test_already_prefixed"


# ----------------------------------------------------------------- render_scaffold


def test_render_scaffold_produces_valid_python():
    """Smoke: emitted content parses as a Python file."""
    import ast

    content = render_scaffold(
        modules={"pkg.domain": ["Shortener"], "pkg.cli": ["main"]},
        deliverables=["Add a URL", "Look up a URL"],
    )
    ast.parse(content)  # raises if not valid Python
    assert "import pytest" in content
    assert "from pkg.domain import Shortener" in content
    assert "from pkg.cli import main" in content
    # Each deliverable gets its own test function with a skip body.
    assert content.count("pytest.skip(") == 2


def test_render_scaffold_fallback_when_no_deliverables():
    content = render_scaffold(modules={}, deliverables=[])
    assert "def test_main_flow()" in content
    assert 'pytest.skip("TODO: no deliverables parsed' in content


def test_render_scaffold_dedupes_duplicate_test_names():
    """Two bullets that snake to the same name get disambiguated."""
    content = render_scaffold(
        modules={},
        deliverables=["Add a URL", "Add a URL."],  # same snake name
    )
    assert content.count("def test_add_a_url") >= 2
    assert "def test_add_a_url_2" in content


def test_render_scaffold_no_imports_when_modules_empty():
    content = render_scaffold(modules={}, deliverables=["X"])
    # Should still have `import pytest` but no `from ... import ...` lines.
    assert "import pytest" in content
    assert "from " not in content


# ------------------------------------------------------- v2.5 contract vs impl mode


def test_render_scaffold_contract_mode_omits_module_level_imports():
    """Contract mode: implementation doesn't exist yet, so module-level
    imports would fail pytest collection. The LLM fills in deferred imports
    inside each test body."""
    content = render_scaffold(
        modules={"pkg.domain": ["Shortener"], "pkg.cli": ["main"]},
        deliverables=["Add a URL", "Look up"],
        mode="contract",
    )
    # Must have `import pytest` at module level.
    assert "import pytest" in content
    # Must NOT have module-level `from pkg...` imports.
    assert "from pkg.domain import" not in content
    assert "from pkg.cli import" not in content
    # Must still parse as Python.
    import ast
    ast.parse(content)
    # Two test functions, one per deliverable.
    assert content.count("pytest.skip(") == 2


def test_render_scaffold_impl_mode_matches_legacy_behavior():
    """Impl mode (default before Sprint 7) retains module-level imports."""
    content = render_scaffold(
        modules={"pkg.domain": ["Shortener"]},
        deliverables=["Add a URL"],
        mode="impl",
    )
    assert "from pkg.domain import Shortener" in content


def test_render_scaffold_contract_mode_docstring_notes_deferred_imports():
    content = render_scaffold(modules={}, deliverables=["X"], mode="contract")
    assert "Contract tests" in content
    assert "deferred" in content.lower()


def test_render_scaffold_api_spec_imports_override_modules():
    """Sprint 7.5: when caller passes api_spec_imports, those are the
    authoritative module-level imports — used regardless of mode."""
    content = render_scaffold(
        modules={},
        deliverables=["Add a URL"],
        mode="contract",
        api_spec_imports=[
            "from src.core_domain import URLShortener",
            "from src.persistence import load, save",
        ],
    )
    # Real imports at module level even though mode=contract.
    assert "from src.core_domain import URLShortener" in content
    assert "from src.persistence import load, save" in content
    # Still has `import pytest`.
    assert "import pytest" in content
    # Parses.
    import ast
    ast.parse(content)


def test_render_scaffold_api_spec_imports_sorted_deterministic():
    content = render_scaffold(
        modules={},
        deliverables=["X"],
        mode="contract",
        api_spec_imports=[
            "from z_mod import thing",
            "from a_mod import other",
        ],
    )
    # Sorted alphabetically for deterministic output.
    assert content.index("from a_mod") < content.index("from z_mod")


def test_render_scaffold_defaults_to_impl_mode():
    """Back-compat default is impl mode (module-level imports included)."""
    content = render_scaffold(
        modules={"pkg.a": ["Thing"]}, deliverables=["X"]
    )
    assert "from pkg.a import Thing" in content


# ----------------------------------------------------------- replace_test_body (Sprint 7.3)


_SCAFFOLD = (
    '"""Tests"""\n'
    "\n"
    "import pytest\n"
    "\n"
    "\n"
    "def test_add():\n"
    '    """Add a URL"""\n'
    '    pytest.skip("TODO: add")\n'
    "\n"
    "\n"
    "def test_lookup():\n"
    '    """Look up a URL"""\n'
    '    pytest.skip("TODO: lookup")\n'
)


def test_replace_body_swaps_skip_for_assertions():
    result = replace_test_body(
        _SCAFFOLD,
        "test_add",
        "from pkg import add\nassert len(add('x')) == 6",
    )
    assert "pytest.skip(\"TODO: add\")" not in result
    # Docstring preserved, body replaced.
    assert "from pkg import add" in result
    assert "assert len(add('x')) == 6" in result
    assert '"""Add a URL"""' in result
    # The OTHER test is untouched.
    assert 'pytest.skip("TODO: lookup")' in result
    # Still valid python.
    import ast as _ast
    _ast.parse(result)


def test_replace_body_dedents_overindented_input():
    """body_code with 8-space indent should be normalized to 4 spaces."""
    result = replace_test_body(
        _SCAFFOLD,
        "test_add",
        "        from pkg import add\n        assert add('x') == 'xxx'",
    )
    # All lines of the body at 4-space indent (one level inside the def).
    for line in result.splitlines():
        if line.startswith("from pkg") or line.startswith("assert add"):
            pytest.fail(f"Expected line to start with 4 spaces: {line!r}")
        if line.startswith("    from pkg") or line.startswith("    assert add"):
            continue  # OK
    assert "    from pkg import add" in result


def test_replace_body_handles_multiline_body():
    result = replace_test_body(
        _SCAFFOLD,
        "test_add",
        "from pkg import add\n"
        "result = add('https://example.com')\n"
        "assert len(result) == 6\n"
        "assert isinstance(result, str)",
    )
    assert "result = add('https://example.com')" in result
    assert "isinstance(result, str)" in result
    import ast as _ast
    _ast.parse(result)


def test_replace_body_preserves_docstring():
    result = replace_test_body(
        _SCAFFOLD, "test_add", "assert True"
    )
    assert '"""Add a URL"""' in result


def test_replace_body_rejects_empty_body_code():
    with pytest.raises(ValueError, match="non-empty"):
        replace_test_body(_SCAFFOLD, "test_add", "")
    with pytest.raises(ValueError, match="non-empty"):
        replace_test_body(_SCAFFOLD, "test_add", "   \n   ")


def test_replace_body_rejects_missing_test_name():
    with pytest.raises(ValueError, match="no function named"):
        replace_test_body(_SCAFFOLD, "test_nonexistent", "assert True")


def test_replace_body_rejects_unparseable_body():
    with pytest.raises(SyntaxError):
        replace_test_body(_SCAFFOLD, "test_add", "def foo(\n")


def test_replace_body_survives_body_without_docstring():
    scaffold_no_doc = (
        "import pytest\n\n"
        "def test_x():\n"
        '    pytest.skip("TODO")\n'
    )
    result = replace_test_body(
        scaffold_no_doc, "test_x", "assert 1 + 1 == 2"
    )
    assert "pytest.skip" not in result
    assert "assert 1 + 1 == 2" in result


def test_replace_body_handles_async_test():
    scaffold = (
        "import pytest\n\n"
        "async def test_async():\n"
        '    """Async stub"""\n'
        '    pytest.skip("TODO")\n'
    )
    result = replace_test_body(scaffold, "test_async", "assert True")
    assert "pytest.skip" not in result
    assert "assert True" in result
