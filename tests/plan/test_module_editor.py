"""Tests for the Sprint 7.4 AST upsert helpers."""

from __future__ import annotations

import ast

import pytest

from agora.plan.module_editor import (
    upsert_class,
    upsert_class_method,
    upsert_function,
)


# =============================================================== upsert_function


def test_upsert_function_appends_to_empty_source():
    result = upsert_function("", "def hello():\n    return 'hi'\n")
    assert "def hello():" in result
    assert "return 'hi'" in result
    ast.parse(result)


def test_upsert_function_appends_when_not_present():
    source = "def existing():\n    pass\n"
    result = upsert_function(source, "def added():\n    return 1\n")
    assert "def existing():" in result
    assert "def added():" in result
    assert result.index("def existing():") < result.index("def added():")
    ast.parse(result)


def test_upsert_function_replaces_existing_in_place():
    """Same-name function gets replaced at the same position, not duplicated."""
    source = (
        "def helper():\n    pass\n\n"
        "def target():\n    return 'old'\n\n"
        "def sibling():\n    pass\n"
    )
    result = upsert_function(
        source, "def target():\n    return 'new'\n"
    )
    # Only ONE def target — the old body is gone.
    assert result.count("def target(") == 1
    assert "return 'new'" in result
    assert "return 'old'" not in result
    # Siblings still present, in original order.
    assert result.index("def helper") < result.index("def target")
    assert result.index("def target") < result.index("def sibling")
    ast.parse(result)


def test_upsert_function_preserves_decorators_on_replacement():
    """If the existing def has decorators, they stay; new def replaces only
    the def block. Actually — we replace the decorator span too, since the
    new code is expected to include any decorators it wants."""
    source = "@staticmethod\ndef target():\n    return 1\n"
    result = upsert_function(
        source, "def target():\n    return 2\n"
    )
    # Old decorator is gone (replaced by new function source).
    assert "@staticmethod" not in result
    assert "return 2" in result


def test_upsert_function_new_def_may_have_decorators():
    source = "x = 1\n"
    result = upsert_function(
        source,
        "@property\ndef target():\n    return 42\n",
    )
    assert "@property" in result
    assert "def target()" in result
    ast.parse(result)


def test_upsert_function_rejects_non_function_code():
    with pytest.raises(ValueError, match="function code must define"):
        upsert_function("", "x = 1\n")


def test_upsert_function_rejects_multiple_defs_in_code():
    with pytest.raises(ValueError, match="exactly one function"):
        upsert_function(
            "",
            "def a():\n    pass\n\ndef b():\n    pass\n",
        )


def test_upsert_function_rejects_syntax_error_in_code():
    with pytest.raises(SyntaxError):
        upsert_function("", "def foo(\n")


def test_upsert_function_rejects_broken_existing_source():
    with pytest.raises(SyntaxError, match="existing module"):
        upsert_function("def broken(\n", "def new():\n    pass\n")


def test_upsert_function_handles_async():
    result = upsert_function(
        "",
        "async def fetch():\n    return await x()\n",
    )
    assert "async def fetch" in result


# ================================================================= upsert_class


def test_upsert_class_appends_to_empty_source():
    result = upsert_class(
        "", "class Shortener:\n    def shorten(self): pass\n"
    )
    assert "class Shortener" in result
    ast.parse(result)


def test_upsert_class_replaces_existing():
    source = (
        "class Shortener:\n    def old_method(self): pass\n\n"
        "x = 1\n"
    )
    result = upsert_class(
        source,
        "class Shortener:\n    def new_method(self): pass\n",
    )
    assert result.count("class Shortener") == 1
    assert "new_method" in result
    assert "old_method" not in result
    assert "x = 1" in result  # siblings preserved


def test_upsert_class_rejects_non_class_code():
    with pytest.raises(ValueError, match="class code must define"):
        upsert_class("", "def foo(): pass\n")


# ======================================================== upsert_class_method


def test_upsert_class_method_appends_to_class():
    source = (
        "class URLShortener:\n"
        "    def __init__(self):\n"
        "        self.mappings = {}\n"
    )
    result = upsert_class_method(
        source,
        "URLShortener",
        "def shorten(self, url: str) -> str:\n    return url[:6]\n",
    )
    # Both methods present under the class.
    tree = ast.parse(result)
    cls = tree.body[0]
    assert isinstance(cls, ast.ClassDef) and cls.name == "URLShortener"
    method_names = [
        n.name for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert method_names == ["__init__", "shorten"]


def test_upsert_class_method_replaces_existing():
    source = (
        "class URLShortener:\n"
        "    def shorten(self, url):\n"
        "        return 'old'\n"
    )
    result = upsert_class_method(
        source,
        "URLShortener",
        "def shorten(self, url: str) -> str:\n    return url[:6]\n",
    )
    assert result.count("def shorten(") == 1
    assert "return url[:6]" in result
    assert "return 'old'" not in result


def test_upsert_class_method_handles_overindented_code():
    """Model often over-indents; framework dedents + re-indents to 4 spaces."""
    source = "class C:\n    def existing(self): pass\n"
    result = upsert_class_method(
        source,
        "C",
        "        def added(self):\n            return 1\n",  # 8-space indent input
    )
    tree = ast.parse(result)
    cls = tree.body[0]
    method = [
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "added"
    ][0]
    # Parsed successfully means indentation was normalized correctly.
    assert method.name == "added"


def test_upsert_class_method_error_when_class_missing():
    source = "x = 1\n"
    with pytest.raises(ValueError, match="class 'URLShortener' not found"):
        upsert_class_method(
            source,
            "URLShortener",
            "def shorten(self): pass\n",
        )


def test_upsert_class_method_error_on_empty_source():
    with pytest.raises(ValueError, match="source is empty"):
        upsert_class_method("", "C", "def m(self): pass\n")


def test_upsert_class_method_preserves_other_methods_and_ordering():
    source = (
        "class Store:\n"
        "    def a(self): return 1\n"
        "    def b(self): return 2\n"
        "    def c(self): return 3\n"
    )
    result = upsert_class_method(
        source, "Store", "def b(self): return 20\n"
    )
    tree = ast.parse(result)
    cls = tree.body[0]
    names = [
        n.name for n in cls.body
        if isinstance(n, ast.FunctionDef)
    ]
    assert names == ["a", "b", "c"]
    assert "return 20" in result
    assert "return 2\n" not in result  # old b body gone


def test_upsert_class_method_no_duplicate_definitions():
    """Regression guard: repeated upsert of the same method must not duplicate."""
    source = "class C:\n    pass\n"
    result = source
    for _ in range(3):
        result = upsert_class_method(
            result, "C",
            "def m(self, x: int) -> int:\n    return x + 1\n",
        )
    assert result.count("def m(") == 1
    tree = ast.parse(result)
    ast.parse(result)  # valid python


def test_upsert_class_method_strips_leading_blank_line_in_code():
    """Observed 7B failure: model sends code='\\n    def __init__():\\n...',
    textwrap.dedent can't dedent because the empty first line has 0
    leading whitespace. _normalize_code strips blank lines first."""
    source = "class C:\n    pass\n"
    result = upsert_class_method(
        source,
        "C",
        "\n    def __init__(self):\n        pass",
    )
    ast.parse(result)
    tree = ast.parse(result)
    cls = tree.body[0]
    method = [
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    ][0]
    assert method.name == "__init__"


def test_upsert_class_method_handles_single_indented_line():
    """Model passes '    def foo(): pass' (single line, 4-space indent).
    textwrap.dedent handles this correctly for a single-line input."""
    source = "class C:\n    pass\n"
    result = upsert_class_method(
        source, "C", "    def __init__(self): pass"
    )
    ast.parse(result)


def test_upsert_class_method_handles_async():
    source = "class API:\n    def sync(self): pass\n"
    result = upsert_class_method(
        source,
        "API",
        "async def fetch(self):\n    return await self._get()\n",
    )
    assert "async def fetch" in result
