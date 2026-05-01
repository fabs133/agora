"""Tests for the Sprint 7.5 API spec parser + scaffolder helpers."""

from __future__ import annotations

import ast

import pytest

from agora.plan.api_spec import (
    ClassSpec,
    FunctionSpec,
    ModuleSpec,
    parse_api_spec,
    render_impl_stub,
    render_test_imports,
)


_SAMPLE_SPEC = """\
# API spec

## module: src/core_domain.py

class URLShortener:
    def __init__(self) -> None: ...
    def shorten(self, url: str) -> str: ...
    def lookup(self, hash: str) -> str | None: ...

def helper(x: int) -> int: ...

## module: src/persistence.py

def save(obj: URLShortener, path: str) -> None: ...
def load(path: str) -> 'URLShortener': ...
"""


# ========================================================= parse_api_spec


def test_parse_extracts_module_sections():
    mods = parse_api_spec(_SAMPLE_SPEC)
    assert [m.path for m in mods] == ["src/core_domain.py", "src/persistence.py"]


def test_parse_extracts_class_with_methods():
    mods = parse_api_spec(_SAMPLE_SPEC)
    core = mods[0]
    assert [c.name for c in core.classes] == ["URLShortener"]
    method_names = [m.name for m in core.classes[0].methods]
    assert method_names == ["__init__", "shorten", "lookup"]


def test_parse_extracts_top_level_functions():
    mods = parse_api_spec(_SAMPLE_SPEC)
    core = mods[0]
    assert [f.name for f in core.functions] == ["helper"]


def test_parse_handles_second_module_functions_only():
    mods = parse_api_spec(_SAMPLE_SPEC)
    persistence = mods[1]
    assert persistence.classes == []
    assert [f.name for f in persistence.functions] == ["save", "load"]


def test_parse_empty_input_returns_empty():
    assert parse_api_spec("") == []
    assert parse_api_spec("# Just a header\nNo module sections.") == []


def test_parse_strips_code_fences():
    spec = """\
## module: src/foo.py

```python
def bar(x: int) -> int: ...
```
"""
    mods = parse_api_spec(spec)
    assert len(mods) == 1
    assert [f.name for f in mods[0].functions] == ["bar"]


def test_parse_skips_malformed_module_bodies():
    """A section with broken python is silently dropped — downstream
    scaffolder falls back to its non-spec path."""
    spec = """\
## module: src/ok.py

def works() -> None: ...

## module: src/broken.py

def broken(:
"""
    mods = parse_api_spec(spec)
    paths = [m.path for m in mods]
    assert "src/ok.py" in paths
    assert "src/broken.py" not in paths


def test_parse_case_insensitive_header():
    spec = "## MODULE: src/x.py\n\ndef a() -> None: ...\n"
    mods = parse_api_spec(spec)
    assert mods[0].path == "src/x.py"


# ============================================================ ModuleSpec props


def test_module_dotted_path():
    m = ModuleSpec(path="src/core_domain.py")
    assert m.dotted == "src.core_domain"


def test_module_dotted_handles_nested():
    m = ModuleSpec(path="src/foo/bar.py")
    assert m.dotted == "src.foo.bar"


def test_module_import_names_includes_classes_and_funcs():
    m = ModuleSpec(
        path="src/x.py",
        classes=[ClassSpec(name="A")],
        functions=[FunctionSpec(name="b", source="def b() -> None: ...")],
    )
    assert set(m.import_names) == {"A", "b"}


# =========================================================== render_impl_stub


def test_render_stub_creates_class_with_notimpl_methods():
    mods = parse_api_spec(_SAMPLE_SPEC)
    stub = render_impl_stub(mods[0])
    # Valid Python.
    ast.parse(stub)
    # Class + methods present.
    assert "class URLShortener:" in stub
    assert "def __init__(self) -> None:" in stub
    assert "def shorten(self, url: str) -> str:" in stub
    # Bodies are NotImplementedError (not `pass` — we want contract tests
    # to fail loudly, not silently pass).
    assert stub.count("raise NotImplementedError") >= 3


def test_render_stub_creates_top_level_functions():
    mods = parse_api_spec(_SAMPLE_SPEC)
    stub = render_impl_stub(mods[0])
    assert "def helper(x: int) -> int:" in stub


def test_render_stub_empty_class_gets_pass():
    m = ModuleSpec(path="src/x.py", classes=[ClassSpec(name="Empty")])
    stub = render_impl_stub(m)
    assert "class Empty:" in stub
    assert "pass" in stub


def test_render_stub_parseable_python():
    """Every stub we emit must be valid python — downstream tools (py_compile,
    add_class_method upsert) rely on this."""
    mods = parse_api_spec(_SAMPLE_SPEC)
    for m in mods:
        stub = render_impl_stub(m)
        ast.parse(stub)


# ================================================= render_test_imports


def test_render_test_imports_emits_one_line_per_module():
    mods = parse_api_spec(_SAMPLE_SPEC)
    lines = render_test_imports(mods)
    assert "from src.core_domain import URLShortener, helper" in lines
    assert "from src.persistence import load, save" in lines


def test_imports_skips_empty_modules():
    empty = ModuleSpec(path="src/empty.py")  # no classes/functions
    lines = render_test_imports([empty])
    assert lines == []


# ================================================= find_unknown_method_calls


def _sample_modules():
    from agora.plan.api_spec import parse_api_spec
    return parse_api_spec(
        "## module: src/url_shortener.py\n\n"
        "class URLShortener:\n"
        "    def __init__(self) -> None: ...\n"
        "    def add_url(self, url: str) -> str: ...\n"
        "    def lookup_hash(self, h: str) -> str: ...\n"
    )


def test_find_unknown_direct_chain():
    from agora.plan.api_spec import find_unknown_method_calls

    source = (
        "from src.url_shortener import URLShortener\n"
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    r = s.add(url)   # wrong: spec says add_url\n"
    )
    v = find_unknown_method_calls(source, _sample_modules())
    assert any("URLShortener.add" in msg for msg in v)


def test_find_unknown_inline_constructor():
    from agora.plan.api_spec import find_unknown_method_calls

    source = (
        "from src.url_shortener import URLShortener\n"
        "def test_y():\n"
        "    r = URLShortener().add(url)   # wrong\n"
    )
    v = find_unknown_method_calls(source, _sample_modules())
    assert any("URLShortener.add" in msg for msg in v)


def test_find_no_violation_when_method_correct():
    from agora.plan.api_spec import find_unknown_method_calls

    source = (
        "from src.url_shortener import URLShortener\n"
        "def test_z():\n"
        "    r = URLShortener().add_url('x')\n"
    )
    v = find_unknown_method_calls(source, _sample_modules())
    assert v == []


def test_find_ignores_non_spec_classes():
    from agora.plan.api_spec import find_unknown_method_calls

    source = (
        "class External:\n"
        "    def foo(self): pass\n"
        "\n"
        "def test_x():\n"
        "    External().bar()   # not a spec class, ignored\n"
    )
    v = find_unknown_method_calls(source, _sample_modules())
    assert v == []


def test_find_ignores_private_attrs():
    from agora.plan.api_spec import find_unknown_method_calls

    source = (
        "from src.url_shortener import URLShortener\n"
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    s._secret   # private, allowed\n"
        "    s.__dict__  # dunder, allowed\n"
    )
    v = find_unknown_method_calls(source, _sample_modules())
    assert v == []


def test_find_dedupes_repeated_violations():
    from agora.plan.api_spec import find_unknown_method_calls

    source = (
        "from src.url_shortener import URLShortener\n"
        "def test_x():\n"
        "    URLShortener().add(1)\n"
        "    URLShortener().add(2)\n"
        "    URLShortener().add(3)\n"
    )
    v = find_unknown_method_calls(source, _sample_modules())
    assert len(v) == 1


def test_find_empty_when_no_spec_classes():
    from agora.plan.api_spec import find_unknown_method_calls

    # A spec with only functions, no classes — nothing to validate.
    from agora.plan.api_spec import parse_api_spec
    modules = parse_api_spec(
        "## module: src/util.py\n\ndef helper(x: int) -> int: ...\n"
    )
    source = "def test_x():\n    some_obj.whatever()\n"
    assert find_unknown_method_calls(source, modules) == []


def test_find_handles_unparseable_source():
    from agora.plan.api_spec import find_unknown_method_calls

    v = find_unknown_method_calls("def broken(\n", _sample_modules())
    assert v == []  # caller handles syntax elsewhere


# ================================================= seed_workspace integration


def test_seed_workspace_writes_stubs_and_spec(tmp_path):
    """End-to-end: seed_workspace writes api_spec.md + matching src/ stubs."""
    from agora.plan.harness import seed_workspace

    # Minimal flow stub with brief + api_spec.
    class _FlowStub:
        brief = "# Brief\n\n## Key deliverables\n- foo\n"
        api_spec = (
            "## module: src/thing.py\n\n"
            "class Thing:\n"
            "    def do_it(self) -> str: ...\n"
            "\n"
            "def helper(x: int) -> int: ...\n"
        )

    seed_workspace(_FlowStub(), "proj", tmp_path)
    project = tmp_path / "proj"
    assert (project / "plan" / "brief.md").is_file()
    assert (project / "plan" / "api_spec.md").is_file()
    stub = (project / "src" / "thing.py").read_text(encoding="utf-8")
    import ast
    ast.parse(stub)
    assert "class Thing:" in stub
    assert "def do_it(self) -> str:" in stub
    assert "def helper(x: int) -> int:" in stub
    assert "raise NotImplementedError" in stub
    # src/__init__.py is auto-created so `src.thing` resolves.
    assert (project / "src" / "__init__.py").is_file()


def test_seed_workspace_no_op_without_api_spec(tmp_path):
    from agora.plan.harness import seed_workspace

    class _FlowStub:
        brief = "# B\n"
        api_spec = ""

    seed_workspace(_FlowStub(), "proj", tmp_path)
    project = tmp_path / "proj"
    assert (project / "plan" / "brief.md").is_file()
    assert not (project / "plan" / "api_spec.md").exists()
    assert not (project / "src").exists()


def test_seed_workspace_respects_existing_stub(tmp_path):
    """If a stub already exists (rerun after partial failure), seed_workspace
    does not overwrite it."""
    from agora.plan.harness import seed_workspace

    class _FlowStub:
        brief = ""
        api_spec = "## module: src/x.py\n\ndef f() -> None: ...\n"

    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "x.py").write_text("# hand-written content\n", encoding="utf-8")
    seed_workspace(_FlowStub(), "proj", tmp_path)
    body = (project / "src" / "x.py").read_text(encoding="utf-8")
    assert "hand-written content" in body


def test_seed_workspace_creates_nested_src_dirs(tmp_path):
    from agora.plan.harness import seed_workspace

    class _FlowStub:
        brief = ""
        api_spec = (
            "## module: src/nested/deep.py\n\n"
            "def go() -> None: ...\n"
        )

    seed_workspace(_FlowStub(), "proj", tmp_path)
    assert (tmp_path / "proj" / "src" / "nested" / "deep.py").is_file()


# ================================================================
# v2.9 / C5 — extract_declared_modules: visibility into sections
# that parse_api_spec silently drops.
# ================================================================


def test_extract_declared_modules_one_section():
    from agora.plan.api_spec import extract_declared_modules

    spec = "## module: src/a.py\n\ndef f() -> None: ...\n"
    declared = extract_declared_modules(spec)
    assert len(declared) == 1
    assert declared[0].path == "src/a.py"
    assert declared[0].is_valid is True
    assert declared[0].parse_error == ""


def test_extract_declared_modules_records_header_line_1_based():
    from agora.plan.api_spec import extract_declared_modules

    spec = (
        "# API spec\n"          # line 1
        "\n"                    # line 2
        "## module: src/a.py\n" # line 3
        "\n"                    # line 4
        "def f() -> None: ...\n"
        "\n"
        "## module: src/b.py\n" # line 7
        "\n"
        "class B: ...\n"
    )
    declared = extract_declared_modules(spec)
    assert len(declared) == 2
    assert declared[0].header_line == 3
    assert declared[1].header_line == 7


def test_extract_declared_modules_catches_syntax_error_in_body():
    from agora.plan.api_spec import extract_declared_modules

    spec = (
        "## module: src/a.py\n\n"
        "- this is a markdown list item, not valid Python\n"
        "def f() -> None: ...\n"
    )
    declared = extract_declared_modules(spec)
    assert len(declared) == 1
    assert declared[0].is_valid is False
    assert "SyntaxError" in declared[0].parse_error


def test_extract_declared_modules_preserves_duplicates():
    """parse_api_spec returns the same path twice too, but the validator
    needs ALL occurrences (with their line numbers) to report them."""
    from agora.plan.api_spec import extract_declared_modules

    spec = (
        "## module: src/a.py\n\n"
        "class A: ...\n\n"
        "## module: src/a.py\n\n"
        "class B: ...\n"
    )
    declared = extract_declared_modules(spec)
    assert len(declared) == 2
    assert declared[0].path == "src/a.py"
    assert declared[1].path == "src/a.py"
    assert declared[0].header_line != declared[1].header_line


def test_extract_declared_modules_empty_body_flagged():
    from agora.plan.api_spec import extract_declared_modules

    spec = "## module: src/a.py\n\n\n## module: src/b.py\n\nclass B: ...\n"
    declared = extract_declared_modules(spec)
    # First has empty body → flagged
    assert declared[0].is_valid is False
    assert "empty" in declared[0].parse_error
    # Second parses cleanly
    assert declared[1].is_valid is True


def test_extract_declared_modules_empty_input_returns_empty():
    from agora.plan.api_spec import extract_declared_modules

    assert extract_declared_modules("") == []
    assert extract_declared_modules("just prose, no headers") == []


# ================================================================
# v2.9 Phase 2+ (auto-heal): strip_test_module_sections
# ================================================================


def test_strip_removes_src_tests_section():
    from agora.plan.api_spec import strip_test_module_sections

    spec = (
        "# API spec\n\n"
        "## module: src/url_shortener.py\n\n"
        "class URLShortener:\n"
        "    def add(self, url: str) -> str: ...\n\n"
        "## module: src/tests/test_url_shortener.py\n\n"
        "# Tests should not be included in the API spec\n"
    )
    cleaned, removed = strip_test_module_sections(spec)
    assert removed == ["src/tests/test_url_shortener.py"]
    assert "src/tests/" not in cleaned
    # Production section preserved verbatim.
    assert "class URLShortener:" in cleaned
    assert "def add(self, url: str) -> str: ..." in cleaned


def test_strip_removes_tests_section():
    """``tests/test_x.py`` (without src/) prefix is also a test path."""
    from agora.plan.api_spec import strip_test_module_sections

    spec = (
        "## module: src/a.py\n\n"
        "class A: ...\n\n"
        "## module: tests/test_a.py\n\n"
        "# comment\n"
    )
    cleaned, removed = strip_test_module_sections(spec)
    assert removed == ["tests/test_a.py"]
    assert "tests/test_a.py" not in cleaned


def test_strip_removes_multiple_test_sections():
    from agora.plan.api_spec import strip_test_module_sections

    spec = (
        "## module: src/a.py\n\nclass A: ...\n\n"
        "## module: src/tests/test_a.py\n\n# test body\n\n"
        "## module: src/b.py\n\nclass B: ...\n\n"
        "## module: tests/test_b.py\n\n# body\n"
    )
    cleaned, removed = strip_test_module_sections(spec)
    assert len(removed) == 2
    assert "class A:" in cleaned
    assert "class B:" in cleaned
    assert "test_a.py" not in cleaned
    assert "test_b.py" not in cleaned


def test_strip_noop_when_no_test_sections():
    from agora.plan.api_spec import strip_test_module_sections

    spec = (
        "## module: src/a.py\n\n"
        "class A: ...\n"
    )
    cleaned, removed = strip_test_module_sections(spec)
    assert removed == []
    assert cleaned == spec


def test_strip_handles_empty_input():
    from agora.plan.api_spec import strip_test_module_sections

    assert strip_test_module_sections("") == ("", [])


def test_strip_preserves_module_ordering():
    from agora.plan.api_spec import strip_test_module_sections

    spec = (
        "## module: src/a.py\n\nclass A: ...\n\n"
        "## module: src/tests/test.py\n\n# body\n\n"
        "## module: src/b.py\n\nclass B: ...\n"
    )
    cleaned, _ = strip_test_module_sections(spec)
    # b comes after a even though test was in between
    a_idx = cleaned.index("class A:")
    b_idx = cleaned.index("class B:")
    assert a_idx < b_idx


def test_strip_case_insensitive_module_header():
    """``## Module:`` (capital M) is equivalent to ``## module:`` per the
    case-insensitive regex in parse_api_spec — strip must match too."""
    from agora.plan.api_spec import strip_test_module_sections

    spec = (
        "## module: src/a.py\n\n"
        "class A: ...\n\n"
        "## Module: src/tests/test.py\n\n"
        "# comment\n"
    )
    cleaned, removed = strip_test_module_sections(spec)
    # Case-insensitive header — strip should catch this too.
    assert removed == ["src/tests/test.py"] or removed == []  # accept either
    # Actually let's be strict — case-insensitive IS the expected behaviour.
    assert "src/tests/test.py" not in cleaned
