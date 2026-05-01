"""Tests for the structural model infrastructure + L1 extractor.

Phase 1: core dataclasses, type annotation parsing, and
:func:`extract_contract` round-trip against api_spec text.
"""

from __future__ import annotations

from agora.plan.structure import (
    Mode,
    TypeKind,
    TypeRef,
    Violation,
    _annotation_to_typeref,
    extract_contract,
    filter_by_mode,
)

# --------------------------------------------------------- TypeRef.describe


def test_typeref_describe_primitive():
    assert TypeRef.primitive("str").describe() == "str"
    assert TypeRef.primitive("int").describe() == "int"


def test_typeref_describe_none():
    assert TypeRef.none().describe() == "None"


def test_typeref_describe_list():
    ref = TypeRef(kind=TypeKind.LIST, params=(TypeRef.primitive("str"),))
    assert ref.describe() == "list[str]"


def test_typeref_describe_dict():
    ref = TypeRef(
        kind=TypeKind.DICT,
        params=(TypeRef.primitive("str"), TypeRef.primitive("int")),
    )
    assert ref.describe() == "dict[str, int]"


def test_typeref_describe_tuple_fixed():
    ref = TypeRef(
        kind=TypeKind.TUPLE,
        params=(TypeRef.primitive("str"), TypeRef.primitive("str")),
    )
    assert ref.describe() == "tuple[str, str]"


def test_typeref_describe_tuple_variadic():
    ref = TypeRef(
        kind=TypeKind.TUPLE,
        params=(TypeRef.primitive("int"),),
        tuple_variadic=True,
    )
    assert ref.describe() == "tuple[int, ...]"


def test_typeref_describe_optional():
    ref = TypeRef(kind=TypeKind.OPTIONAL, params=(TypeRef.primitive("str"),))
    assert ref.describe() == "Optional[str]"


def test_typeref_describe_class_ref():
    assert TypeRef.cls_ref("URLShortener").describe() == "URLShortener"


def test_typeref_describe_unknown_with_hint():
    assert TypeRef.unknown("Foo.Bar").describe() == "Foo.Bar"


# -------------------------------------------------- _annotation_to_typeref


def _parse_annotation(source: str, declared: set[str] | None = None) -> TypeRef:
    """Utility: parse a single annotation expression via the real helper."""
    import ast

    tree = ast.parse(f"x: {source}")
    assign = tree.body[0]
    assert hasattr(assign, "annotation")
    return _annotation_to_typeref(assign.annotation, declared or set())


def test_annotation_primitive_str():
    t = _parse_annotation("str")
    assert t.kind is TypeKind.PRIMITIVE
    assert t.name == "str"


def test_annotation_primitive_int():
    assert _parse_annotation("int").kind is TypeKind.PRIMITIVE


def test_annotation_none():
    assert _parse_annotation("None").kind is TypeKind.NONE


def test_annotation_list_of_str():
    t = _parse_annotation("list[str]")
    assert t.kind is TypeKind.LIST
    assert t.params[0].kind is TypeKind.PRIMITIVE
    assert t.params[0].name == "str"


def test_annotation_list_of_tuple():
    t = _parse_annotation("list[tuple[str, str]]")
    assert t.kind is TypeKind.LIST
    inner = t.params[0]
    assert inner.kind is TypeKind.TUPLE
    assert [p.name for p in inner.params] == ["str", "str"]


def test_annotation_dict_of_str_to_int():
    t = _parse_annotation("dict[str, int]")
    assert t.kind is TypeKind.DICT
    assert t.params[0].name == "str"
    assert t.params[1].name == "int"


def test_annotation_tuple_variadic():
    t = _parse_annotation("tuple[int, ...]")
    assert t.kind is TypeKind.TUPLE
    assert t.tuple_variadic is True
    assert t.params[0].name == "int"


def test_annotation_optional_explicit():
    t = _parse_annotation("Optional[str]")
    assert t.kind is TypeKind.OPTIONAL
    assert t.params[0].name == "str"


def test_annotation_optional_via_pep_604():
    """`str | None` folds into Optional[str] at the model level."""
    t = _parse_annotation("str | None")
    assert t.kind is TypeKind.OPTIONAL
    assert t.params[0].name == "str"


def test_annotation_union_pep_604():
    t = _parse_annotation("str | int")
    assert t.kind is TypeKind.UNION
    # Order preserved from left to right in the source.
    assert [p.name for p in t.params] == ["str", "int"]


def test_annotation_class_reference():
    t = _parse_annotation("URLShortener", declared={"URLShortener"})
    assert t.kind is TypeKind.CLASS
    assert t.name == "URLShortener"


def test_annotation_unresolved_bare_name_becomes_unknown():
    """Bare name we can't resolve (not primitive, not declared) →
    Unknown with the name as a hint. Caller decides how strict to be."""
    t = _parse_annotation("MyCustomAlias")
    assert t.kind is TypeKind.UNKNOWN
    assert t.name == "MyCustomAlias"


def test_annotation_any_explicit():
    t = _parse_annotation("Any")
    assert t.kind is TypeKind.ANY


def test_annotation_complex_subscripted_unknown():
    """Callable[[int], str] → Unknown (we don't model function types)."""
    t = _parse_annotation("Callable[[int], str]")
    assert t.kind is TypeKind.UNKNOWN


# --------------------------------------------------------- extract_contract


def test_extract_contract_single_module():
    spec = (
        "## module: src/shortener.py\n\n"
        "class URLShortener:\n"
        "    def __init__(self) -> None: ...\n"
        "    def add_url(self, long_url: str) -> str: ...\n"
        "    def lookup(self, short_hash: str) -> Optional[str]: ...\n"
        "    def list_mappings(self) -> list[tuple[str, str]]: ...\n"
    )
    contract = extract_contract(spec)
    assert len(contract.modules) == 1
    mod = contract.modules[0]
    assert mod.path == "src/shortener.py"
    assert len(mod.classes) == 1
    cls = mod.classes[0]
    assert cls.name == "URLShortener"
    assert [m.name for m in cls.methods] == [
        "__init__",
        "add_url",
        "lookup",
        "list_mappings",
    ]


def test_extract_contract_captures_return_types():
    spec = (
        "## module: src/s.py\n\n"
        "class S:\n"
        "    def get_str(self) -> str: ...\n"
        "    def get_items(self) -> list[tuple[str, str]]: ...\n"
        "    def get_optional(self) -> Optional[str]: ...\n"
    )
    contract = extract_contract(spec)
    methods = contract.modules[0].classes[0].methods
    by_name = {m.name: m.return_type for m in methods}
    assert by_name["get_str"].kind is TypeKind.PRIMITIVE
    assert by_name["get_items"].kind is TypeKind.LIST
    assert by_name["get_items"].params[0].kind is TypeKind.TUPLE
    assert by_name["get_optional"].kind is TypeKind.OPTIONAL


def test_extract_contract_captures_param_types():
    spec = (
        "## module: src/s.py\n\n"
        "class S:\n"
        "    def save(self, path: str, overwrite: bool) -> None: ...\n"
    )
    contract = extract_contract(spec)
    method = contract.modules[0].classes[0].methods[0]
    assert method.name == "save"
    assert [(n, t.name) for n, t in method.params] == [
        ("path", "str"),
        ("overwrite", "bool"),
    ]
    assert method.return_type.kind is TypeKind.NONE


def test_extract_contract_top_level_function_no_self():
    spec = (
        "## module: src/util.py\n\n"
        "def helper(x: int) -> int: ...\n"
    )
    contract = extract_contract(spec)
    mod = contract.modules[0]
    assert len(mod.functions) == 1
    fn = mod.functions[0]
    assert fn.name == "helper"
    assert fn.has_self is False


def test_extract_contract_empty_spec_yields_empty_model():
    assert extract_contract("").modules == ()


def test_extract_contract_multiple_modules():
    spec = (
        "## module: src/a.py\n\n"
        "class A:\n    def f(self) -> None: ...\n\n"
        "## module: src/b.py\n\n"
        "class B:\n    def g(self) -> int: ...\n"
    )
    contract = extract_contract(spec)
    assert [m.path for m in contract.modules] == ["src/a.py", "src/b.py"]


def test_contract_all_class_names():
    spec = (
        "## module: src/a.py\n\n"
        "class Foo:\n    def f(self) -> None: ...\n\n"
        "## module: src/b.py\n\n"
        "class Bar:\n    def g(self) -> None: ...\n"
    )
    contract = extract_contract(spec)
    assert contract.all_class_names() == {"Foo", "Bar"}


def test_contract_find_class_across_modules():
    spec = (
        "## module: src/a.py\n\nclass Foo:\n    def f(self) -> None: ...\n\n"
        "## module: src/b.py\n\nclass Bar:\n    def g(self) -> None: ...\n"
    )
    contract = extract_contract(spec)
    found = contract.find_class("Bar")
    assert found is not None
    mod, cls = found
    assert mod.path == "src/b.py"
    assert cls.name == "Bar"


def test_contract_declared_class_references_resolve():
    """A method returning a declared class should produce a CLASS TypeRef,
    not Unknown — cross-class references inside a spec work."""
    spec = (
        "## module: src/a.py\n\n"
        "class Foo: ...\n"
        "class Factory:\n    def make(self) -> Foo: ...\n"
    )
    contract = extract_contract(spec)
    factory = contract.find_class("Factory")[1]
    make = factory.find_method("make")
    assert make.return_type.kind is TypeKind.CLASS
    assert make.return_type.name == "Foo"


# --------------------------------------------------------- Mode / filter


def test_filter_by_mode_permissive_drops_unresolved():
    vs = [
        Violation(path="x", message="obvious", severity="certain"),
        Violation(path="y", message="unknown type", severity="unresolved"),
    ]
    kept = filter_by_mode(vs, Mode.PERMISSIVE)
    assert len(kept) == 1
    assert kept[0].severity == "certain"


def test_filter_by_mode_strict_keeps_both():
    vs = [
        Violation(path="x", message="obvious", severity="certain"),
        Violation(path="y", message="unknown type", severity="unresolved"),
    ]
    kept = filter_by_mode(vs, Mode.STRICT)
    assert len(kept) == 2


# ================================================================
# Phase 2 — L2 extractor + L1↔L2 diff
# ================================================================

from agora.plan.structure import (
    check_usage_matches_contract,
    extract_usage_traces,
)

_SHORTENER_SPEC = (
    "## module: src/url_shortener.py\n\n"
    "class URLShortener:\n"
    "    def __init__(self) -> None: ...\n"
    "    def add_url(self, long_url: str) -> str: ...\n"
    "    def lookup(self, short_hash: str) -> str: ...\n"
    "    def list_mappings(self) -> list[str]: ...\n"
    "    def list_tuples(self) -> list[tuple[str, str]]: ...\n"
    "    def count(self) -> int: ...\n"
    "    def describe(self) -> dict[str, int]: ...\n"
    "    def maybe(self) -> Optional[str]: ...\n"
    "    def save(self) -> None: ...\n"
)


def _violations(test_source: str, spec_text: str = _SHORTENER_SPEC) -> list:
    """Helper: extract permissive-mode violations from test source."""
    contract = extract_contract(spec_text)
    traces = extract_usage_traces(test_source, contract)
    vs = check_usage_matches_contract(traces, contract)
    return filter_by_mode(vs, Mode.PERMISSIVE)


# --------------------------------------------------------- extract_usage_traces


def test_usage_traces_captures_constructor_chain():
    src = (
        "def test_x():\n"
        "    result = URLShortener().add_url('http://a.com')\n"
    )
    contract = extract_contract(_SHORTENER_SPEC)
    traces = extract_usage_traces(src, contract)
    assert len(traces) == 1
    t = traces[0]
    assert t.class_name == "URLShortener"
    assert t.method_name == "add_url"


def test_usage_traces_resolves_local_alias():
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    result = s.add_url('http://a.com')\n"
    )
    contract = extract_contract(_SHORTENER_SPEC)
    traces = extract_usage_traces(src, contract)
    method_names = [t.method_name for t in traces]
    assert "add_url" in method_names


def test_usage_traces_captures_subscript_chain():
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    items = s.list_mappings()\n"
        "    first = items[0]\n"
    )
    contract = extract_contract(_SHORTENER_SPEC)
    traces = extract_usage_traces(src, contract)
    # One trace for the bare call, one trace for items[0]
    # rooted at list_mappings.
    indexed = [t for t in traces if len(t.steps) > 0]
    assert len(indexed) >= 1
    t = indexed[0]
    assert t.steps[0].kind == "subscript_index"


def test_usage_traces_captures_comprehension_element():
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    mappings = s.list_mappings()\n"
        "    _ = [m['hash'] for m in mappings]\n"
    )
    contract = extract_contract(_SHORTENER_SPEC)
    traces = extract_usage_traces(src, contract)
    # The m['hash'] access should produce a trace rooted at list_mappings
    # with an iter step followed by the dict subscript.
    with_iter = [
        t for t in traces
        if any(s.kind == "iter" for s in t.steps)
    ]
    assert with_iter, f"expected at least one iter-rooted trace, got {traces}"
    step_kinds = [s.kind for s in with_iter[0].steps]
    assert "iter" in step_kinds
    assert "subscript_str" in step_kinds


def test_usage_traces_ignores_unrelated_calls():
    """Calls on objects outside the spec are not captured."""
    src = (
        "def test_x():\n"
        "    data = {'a': 1}\n"
        "    result = data.get('a')\n"
    )
    contract = extract_contract(_SHORTENER_SPEC)
    traces = extract_usage_traces(src, contract)
    assert traces == []


def test_usage_traces_skips_malformed_source():
    src = "def test_x:\n    this is not python"
    contract = extract_contract(_SHORTENER_SPEC)
    traces = extract_usage_traces(src, contract)
    assert traces == []


# --------------------------------------------------------- L1↔L2: happy paths


def test_diff_list_str_index_ok():
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    first = s.list_mappings()[0]\n"
    )
    assert _violations(src) == []


def test_diff_list_str_iter_ok():
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    for m in s.list_mappings():\n"
        "        assert isinstance(m, str)\n"
    )
    # Direct iteration via for-stmt: we currently only handle
    # iteration in comprehensions. But no VIOLATION should fire —
    # permissive default. Just verify no false positive.
    assert _violations(src) == []


def test_diff_list_tuple_index_into_tuple_ok():
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    first = s.list_tuples()[0][1]\n"
    )
    # list[tuple[str, str]][0] → tuple[str, str], [1] → str. OK.
    assert _violations(src) == []


def test_diff_dict_str_int_string_key_ok():
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    n = s.describe()['items']\n"
    )
    assert _violations(src) == []


def test_diff_str_method_upper_ok():
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    assert s.lookup('abc').upper() == 'ABC'\n"
    )
    assert _violations(src) == []


def test_diff_str_int_index_ok():
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    first_char = s.lookup('abc')[0]\n"
    )
    assert _violations(src) == []


def test_diff_optional_str_is_unwrapped_for_check():
    """Optional[str] is treated as str for step analysis."""
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    val = s.maybe()\n"
        "    assert val.upper() == 'X'\n"
    )
    # No violation — we treat Optional[str] as str for structural check.
    assert _violations(src) == []


# --------------------------------------------------------- L1↔L2: violations


def test_diff_list_str_string_subscript_violation():
    """The exact failure from 2026-04-22: tester did `mappings[0]['hash']`
    on a list[str]. Must be caught as a certain violation."""
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    first = s.list_mappings()[0]['hash']\n"
    )
    violations = _violations(src)
    assert len(violations) >= 1
    assert "strings only accept int" in violations[0].message.lower() or \
           "string key" in violations[0].message.lower()


def test_diff_list_iter_then_dict_subscript_violation():
    """Iterating list[str] then `m['hash']` — the compound pattern."""
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    _ = [m['hash'] for m in s.list_mappings()]\n"
    )
    # Note: the extractor aliases list_mappings return via `mappings=...`
    # pattern; for comprehension-over-method-return, the direct-call
    # pattern isn't tracked. Verify the deferred pattern works:
    src2 = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    mappings = s.list_mappings()\n"
        "    _ = [m['hash'] for m in mappings]\n"
    )
    violations = _violations(src2)
    assert len(violations) >= 1


def test_diff_list_string_key_directly_violation():
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    _ = s.list_mappings()['hash']\n"
    )
    violations = _violations(src)
    assert len(violations) >= 1
    assert "list" in violations[0].message.lower()
    assert "string key" in violations[0].message.lower()


def test_diff_int_subscript_violation():
    """Subscripting an int is a certain violation."""
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    _ = s.count()[0]\n"
    )
    violations = _violations(src)
    assert len(violations) >= 1
    assert "int" in violations[0].message.lower()


def test_diff_none_return_attribute_violation():
    """Method returns None; accessing anything on it is invalid."""
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    _ = s.save().then()\n"
    )
    violations = _violations(src)
    assert len(violations) >= 1
    assert "none" in violations[0].message.lower()


def test_diff_dict_wrong_key_type_violation():
    """dict[str, int] subscripted with int — certain violation."""
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    _ = s.describe()[0]\n"
    )
    violations = _violations(src)
    assert len(violations) >= 1
    assert "integer index" in violations[0].message.lower()


# --------------------------------------------------------- strict mode


def test_strict_mode_surfaces_unresolved_types():
    """Method with no return annotation → return type is Unknown.
    Permissive permits any access; strict flags it."""
    spec = (
        "## module: src/x.py\n\n"
        "class Mystery:\n"
        "    def oracle(self): ...\n"   # no return annotation
    )
    src = (
        "def test_x():\n"
        "    _ = Mystery().oracle()[0]['key']\n"
    )
    contract = extract_contract(spec)
    traces = extract_usage_traces(src, contract)
    vs = check_usage_matches_contract(traces, contract)
    assert filter_by_mode(vs, Mode.PERMISSIVE) == []  # no certain violations
    strict = filter_by_mode(vs, Mode.STRICT)
    assert any(v.severity == "unresolved" for v in strict)


# --------------------------------------------------------- robustness


def test_diff_unknown_method_is_not_flagged():
    """Calls to methods not in the spec are skipped (other gate catches)."""
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    _ = s.nonexistent()[0]['nope']\n"
    )
    # No violations from us — find_unknown_method_calls handles this.
    assert _violations(src) == []


def test_diff_no_access_no_violation():
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    s.save()\n"
    )
    assert _violations(src) == []


def test_diff_stops_cascading_on_first_violation_per_chain():
    """Once we flag `list_mappings()[0]['bad']`, we don't keep walking
    into downstream accesses — avoids pointlessly long error lists."""
    src = (
        "def test_x():\n"
        "    s = URLShortener()\n"
        "    _ = s.list_mappings()[0]['bad']['worse']['worst']\n"
    )
    violations = _violations(src)
    # Exactly one violation for this chain.
    assert len(violations) == 1


# ================================================================
# Phase 3 — L3 extractor + self-consistency diff
# ================================================================

from agora.plan.structure import (
    check_impl_self_consistent,
    extract_impl_classes,
)


def test_impl_extracts_attrs_set_and_read():
    src = (
        "class A:\n"
        "    def __init__(self):\n"
        "        self.x = 1\n"
        "        self.y = 2\n"
        "    def use(self):\n"
        "        return self.x + self.y\n"
    )
    classes = extract_impl_classes(src)
    assert len(classes) == 1
    a = classes[0]
    assert a.name == "A"
    assert set(a.attrs_set) == {"x", "y"}
    assert set(a.attrs_read) == {"x", "y"}
    assert set(a.method_names) == {"__init__", "use"}


def test_impl_detects_aug_assign():
    src = (
        "class A:\n"
        "    def __init__(self):\n"
        "        self.counter = 0\n"
        "    def inc(self):\n"
        "        self.counter += 1\n"
    )
    classes = extract_impl_classes(src)
    assert "counter" in classes[0].attrs_set
    assert "counter" in classes[0].attrs_read


def test_impl_does_not_confuse_self_attr_with_other():
    """`other.x` should NOT contribute to self's attribute tracking."""
    src = (
        "class A:\n"
        "    def __init__(self):\n"
        "        self.x = 1\n"
        "    def peek(self, other):\n"
        "        return other.x + self.x\n"
    )
    classes = extract_impl_classes(src)
    a = classes[0]
    assert "x" in a.attrs_set
    assert "x" in a.attrs_read  # self.x counted
    # No other keys — other.x is irrelevant to self.


def test_self_consistency_detects_field_typo():
    """The exact 2026-04-22 bug: __init__ sets url_mapping, methods
    read url_hash_map."""
    src = (
        "class URLShortener:\n"
        "    def __init__(self):\n"
        "        self.url_mapping = {}\n"
        "        self.counter = 0\n"
        "    def add_url(self, long_url):\n"
        "        self.counter += 1\n"
        "        hash_code = str(self.counter)\n"
        "        self.url_hash_map[hash_code] = long_url\n"
        "        return hash_code\n"
    )
    classes = extract_impl_classes(src)
    violations = check_impl_self_consistent(classes)
    assert len(violations) == 1
    msg = violations[0].message
    assert "url_hash_map" in msg
    assert "url_mapping" in msg or "self.*" in msg
    assert violations[0].severity == "certain"


def test_self_consistency_no_violation_on_clean_class():
    src = (
        "class A:\n"
        "    def __init__(self):\n"
        "        self.x = 1\n"
        "    def get(self):\n"
        "        return self.x\n"
    )
    classes = extract_impl_classes(src)
    assert check_impl_self_consistent(classes) == []


def test_self_consistency_ignores_method_references():
    """`self.method()` reads the method attribute — must not be flagged."""
    src = (
        "class A:\n"
        "    def go(self):\n"
        "        return self.helper()\n"
        "    def helper(self):\n"
        "        return 1\n"
    )
    classes = extract_impl_classes(src)
    assert check_impl_self_consistent(classes) == []


def test_self_consistency_handles_empty_class():
    src = "class A:\n    pass\n"
    classes = extract_impl_classes(src)
    assert check_impl_self_consistent(classes) == []


def test_self_consistency_multi_class():
    """Each class checked independently — bad B shouldn't flag good A."""
    src = (
        "class A:\n"
        "    def __init__(self):\n"
        "        self.x = 1\n"
        "    def get(self):\n"
        "        return self.x\n"
        "\n"
        "class B:\n"
        "    def __init__(self):\n"
        "        self.y = 1\n"
        "    def get(self):\n"
        "        return self.z\n"  # typo: z was never set
    )
    classes = extract_impl_classes(src)
    violations = check_impl_self_consistent(classes)
    assert len(violations) == 1
    assert "class B" in violations[0].path
    assert "z" in violations[0].message


def test_self_consistency_parse_failure_returns_empty():
    """Malformed source yields no classes — caller uses py_compiles for syntax."""
    assert extract_impl_classes("def broken(:") == []


def test_self_consistency_private_attrs_tracked_same():
    src = (
        "class A:\n"
        "    def __init__(self):\n"
        "        self._a = 1\n"
        "    def use(self):\n"
        "        return self._b\n"  # typo: should be _a
    )
    violations = check_impl_self_consistent(extract_impl_classes(src))
    assert len(violations) == 1
    assert "_b" in violations[0].message


# --------------------------------------------------------- predicate registry


def test_class_attributes_consistent_via_registry(tmp_path):
    """Predicate is registered + evaluates against a src/*.py file."""
    from agora.plan.predicate_registry import build_predicate

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "class A:\n"
        "    def __init__(self):\n"
        "        self.x = 1\n"
        "    def use(self):\n"
        "        return self.y\n",  # violation
        encoding="utf-8",
    )
    pred = build_predicate(
        "class_attributes_consistent", {"rel": "src/app.py"}
    )
    passed, reason = pred.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "y" in reason


def test_class_attributes_consistent_passes_clean_file(tmp_path):
    from agora.plan.predicate_registry import build_predicate

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "class A:\n"
        "    def __init__(self):\n"
        "        self.x = 1\n"
        "    def use(self):\n"
        "        return self.x\n",
        encoding="utf-8",
    )
    pred = build_predicate(
        "class_attributes_consistent", {"rel": "src/app.py"}
    )
    passed, _ = pred.evaluate({"work_dir": str(tmp_path)})
    assert passed is True


def test_class_attributes_consistent_missing_file_fails(tmp_path):
    from agora.plan.predicate_registry import build_predicate

    pred = build_predicate(
        "class_attributes_consistent", {"rel": "src/nope.py"}
    )
    passed, reason = pred.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "does not exist" in reason


def test_class_attributes_consistent_no_classes_passes(tmp_path):
    """A file with only top-level functions — nothing to check."""
    from agora.plan.predicate_registry import build_predicate

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "util.py").write_text(
        "def helper(x):\n    return x + 1\n", encoding="utf-8"
    )
    pred = build_predicate(
        "class_attributes_consistent", {"rel": "src/util.py"}
    )
    passed, _ = pred.evaluate({"work_dir": str(tmp_path)})
    assert passed is True
