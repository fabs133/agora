"""Structural models of plan artifacts + comparators for cross-layer drift.

Three layers of "what the system believes a module looks like":

  L1 — **Contract model** (:class:`ContractModel`): derived from
       ``plan/api_spec.md``. Ground truth — what the plan commits to.

  L2 — **Usage-implied traces** (:class:`UsageTrace`): derived from test
       bodies. What the tester ASSUMES each method's return looks like
       based on how they access it.

  L3 — **Implementation-actual model** (:class:`ImplClassModel`):
       derived from ``src/*.py`` AST after the implementer writes. What
       the code actually exposes.

Comparators diff these pairwise. Every comparator returns a list of
:class:`Violation` objects with ``severity`` ∈ ``{"certain", "unresolved"}``.

  - ``certain``: an obvious contradiction (e.g. ``list[str][0]['key']``
    when key-subscript on a string is invalid).
  - ``unresolved``: the comparator couldn't figure something out
    because a type was ``Unknown``, a variable was aliased through
    multiple assignments, etc.

Callers can filter: **permissive mode** reports only ``certain``;
**strict mode** reports both. Default is permissive so false positives
don't wreck productive runs; strict is an opt-in stress-test surface.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

# ============================================================
# Type model
# ============================================================


class TypeKind(str, Enum):
    """What category of type a :class:`TypeRef` represents.

    Kept deliberately coarse — this module is not mypy. Anything the
    parser can't categorise becomes :attr:`UNKNOWN` and comparators
    treat it as unresolved (permissive by default).
    """

    PRIMITIVE = "primitive"   # str, int, bool, float
    NONE = "none"             # None / NoneType
    LIST = "list"             # list[X]
    TUPLE = "tuple"           # tuple[A, B, ...] or tuple[X, ...]
    DICT = "dict"             # dict[K, V]
    CLASS = "class"           # a declared class name in the spec
    OPTIONAL = "optional"     # Optional[X] ≡ X | None
    UNION = "union"           # X | Y | Z
    ANY = "any"               # explicit `Any` annotation
    UNKNOWN = "unknown"       # anything we couldn't resolve


@dataclass(frozen=True)
class TypeRef:
    """A parsed representation of a type annotation.

    Only the fields relevant to ``kind`` are populated:

    - ``PRIMITIVE``: ``name`` (``"str"``, ``"int"``, ``"bool"``, ``"float"``)
    - ``LIST``: ``params=(element_type,)``
    - ``TUPLE``: ``params=(t0, t1, ...)`` with optional trailing Ellipsis
      marker via :attr:`tuple_variadic`
    - ``DICT``: ``params=(key_type, value_type)``
    - ``CLASS``: ``name`` = declared class name
    - ``OPTIONAL`` / ``UNION``: ``params=(t0, t1, ...)``
    - ``NONE`` / ``ANY`` / ``UNKNOWN``: no additional info
    """

    kind: TypeKind
    name: str = ""
    params: tuple[TypeRef, ...] = ()
    tuple_variadic: bool = False  # tuple[X, ...] with literal ellipsis

    # Convenience constructors keep call sites readable.

    @classmethod
    def primitive(cls, name: str) -> TypeRef:
        return cls(kind=TypeKind.PRIMITIVE, name=name)

    @classmethod
    def cls_ref(cls, name: str) -> TypeRef:
        return cls(kind=TypeKind.CLASS, name=name)

    @classmethod
    def none(cls) -> TypeRef:
        return cls(kind=TypeKind.NONE, name="None")

    @classmethod
    def unknown(cls, hint: str = "") -> TypeRef:
        return cls(kind=TypeKind.UNKNOWN, name=hint)

    @classmethod
    def any_(cls) -> TypeRef:
        return cls(kind=TypeKind.ANY, name="Any")

    def describe(self) -> str:
        """Short human-readable form for error messages."""
        if self.kind is TypeKind.PRIMITIVE:
            return self.name
        if self.kind is TypeKind.NONE:
            return "None"
        if self.kind is TypeKind.ANY:
            return "Any"
        if self.kind is TypeKind.UNKNOWN:
            return self.name or "<unknown>"
        if self.kind is TypeKind.CLASS:
            return self.name
        if self.kind is TypeKind.LIST:
            inner = self.params[0].describe() if self.params else "?"
            return f"list[{inner}]"
        if self.kind is TypeKind.DICT:
            k = self.params[0].describe() if len(self.params) > 0 else "?"
            v = self.params[1].describe() if len(self.params) > 1 else "?"
            return f"dict[{k}, {v}]"
        if self.kind is TypeKind.TUPLE:
            parts = ", ".join(p.describe() for p in self.params)
            if self.tuple_variadic:
                parts += ", ..."
            return f"tuple[{parts}]"
        if self.kind is TypeKind.OPTIONAL:
            inner = self.params[0].describe() if self.params else "?"
            return f"Optional[{inner}]"
        if self.kind is TypeKind.UNION:
            return " | ".join(p.describe() for p in self.params)
        return "?"


@dataclass(frozen=True)
class MethodSignature:
    name: str
    params: tuple[tuple[str, TypeRef], ...] = ()  # excludes self
    return_type: TypeRef = field(default_factory=TypeRef.unknown)
    has_self: bool = True


@dataclass(frozen=True)
class ClassModel:
    name: str
    methods: tuple[MethodSignature, ...] = ()

    def find_method(self, method_name: str) -> MethodSignature | None:
        for m in self.methods:
            if m.name == method_name:
                return m
        return None


@dataclass(frozen=True)
class ModuleModel:
    path: str
    classes: tuple[ClassModel, ...] = ()
    functions: tuple[MethodSignature, ...] = ()

    def find_class(self, class_name: str) -> ClassModel | None:
        for c in self.classes:
            if c.name == class_name:
                return c
        return None


@dataclass(frozen=True)
class ContractModel:
    """L1: everything api_spec declares. Frozen — deterministic from text."""

    modules: tuple[ModuleModel, ...] = ()

    def all_class_names(self) -> set[str]:
        return {c.name for m in self.modules for c in m.classes}

    def find_class(self, class_name: str) -> tuple[ModuleModel, ClassModel] | None:
        for mod in self.modules:
            cls = mod.find_class(class_name)
            if cls is not None:
                return (mod, cls)
        return None


# ============================================================
# Violation / mode
# ============================================================


Severity = Literal["certain", "unresolved"]


@dataclass(frozen=True)
class Violation:
    """A cross-layer drift the comparator detected.

    ``severity`` controls whether permissive mode surfaces this. ``path``
    is a human-oriented breadcrumb (e.g. ``"test_foo → URLShortener.bar()
    → [0] → ['key']"``) so the error message points precisely at the
    offending chain.
    """

    path: str
    message: str
    severity: Severity = "certain"


class Mode(str, Enum):
    PERMISSIVE = "permissive"   # report only certain violations
    STRICT = "strict"            # report both certain and unresolved


def filter_by_mode(violations: list[Violation], mode: Mode) -> list[Violation]:
    if mode is Mode.STRICT:
        return list(violations)
    return [v for v in violations if v.severity == "certain"]


# ============================================================
# L1 extractor — api_spec.md → ContractModel
# ============================================================


def extract_contract(spec_text: str) -> ContractModel:
    """Parse :mod:`agora.plan.api_spec` output into a structured model.

    Uses :func:`agora.plan.api_spec.parse_api_spec` as the source of
    module boundaries + method bodies, then walks each method's source
    to extract a :class:`MethodSignature` with typed params + return.
    """
    # Local import avoids a circular dependency at module load time.
    from agora.plan.api_spec import parse_api_spec

    modules_raw = parse_api_spec(spec_text)
    declared_classes = {
        c.name for m in modules_raw for c in m.classes
    }

    mods: list[ModuleModel] = []
    for m_raw in modules_raw:
        classes = [
            _extract_class_from_spec(c_raw, declared_classes)
            for c_raw in m_raw.classes
        ]
        fns = [
            _extract_signature_from_source(fn_raw.source, declared_classes)
            for fn_raw in m_raw.functions
        ]
        mods.append(
            ModuleModel(
                path=m_raw.path,
                classes=tuple(classes),
                functions=tuple(fns),
            )
        )
    return ContractModel(modules=tuple(mods))


def _extract_class_from_spec(cls_raw, declared: set[str]) -> ClassModel:
    methods: list[MethodSignature] = []
    for m in cls_raw.methods:
        methods.append(_extract_signature_from_source(m.source, declared))
    return ClassModel(name=cls_raw.name, methods=tuple(methods))


def _extract_signature_from_source(
    source: str, declared_classes: set[str]
) -> MethodSignature:
    """Parse a ``def foo(...) -> X: ...`` source string into a signature."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return MethodSignature(
            name="<unparsed>",
            return_type=TypeRef.unknown("<parse error>"),
        )
    if not tree.body:
        return MethodSignature(name="<empty>")
    fn = tree.body[0]
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return MethodSignature(name="<not-a-function>")

    args = fn.args.args
    has_self = bool(args) and args[0].arg == "self"
    payload_args = args[1:] if has_self else args[:]
    params: list[tuple[str, TypeRef]] = []
    for a in payload_args:
        if a.annotation is None:
            params.append((a.arg, TypeRef.unknown("<no annotation>")))
        else:
            params.append((a.arg, _annotation_to_typeref(a.annotation, declared_classes)))

    if fn.returns is None:
        return_type = TypeRef.unknown("<no return annotation>")
    else:
        return_type = _annotation_to_typeref(fn.returns, declared_classes)
    return MethodSignature(
        name=fn.name,
        params=tuple(params),
        return_type=return_type,
        has_self=has_self,
    )


_PRIMITIVES = {"str", "int", "bool", "float", "bytes", "complex"}


def _annotation_to_typeref(node: ast.AST, declared_classes: set[str]) -> TypeRef:
    """Convert an annotation AST node into a :class:`TypeRef`.

    Supports: bare names, ``list[X]``, ``dict[K, V]``, ``tuple[A, B, ...]``,
    ``Optional[X]``, ``X | Y`` union syntax, ``None``, ``Any``. Everything
    else returns :meth:`TypeRef.unknown`.
    """
    # None literal (Python 3.12 parses `None` as ast.Constant(value=None))
    if isinstance(node, ast.Constant) and node.value is None:
        return TypeRef.none()

    # Simple name: str / int / MyClass / None (legacy) / Any / ...
    if isinstance(node, ast.Name):
        name = node.id
        if name == "None":
            return TypeRef.none()
        if name == "Any":
            return TypeRef.any_()
        if name in _PRIMITIVES:
            return TypeRef.primitive(name)
        if name in declared_classes:
            return TypeRef.cls_ref(name)
        # Bare name we can't resolve — typing.Optional without the import,
        # or an alias. Caller will treat as unresolved.
        return TypeRef.unknown(name)

    # Subscript: list[X], dict[K, V], tuple[A, B], Optional[X], Union[...]
    if isinstance(node, ast.Subscript):
        base = node.value
        slice_node = node.slice
        base_name = _name_of(base)
        if base_name in ("list", "List"):
            inner = _annotation_to_typeref(slice_node, declared_classes)
            return TypeRef(kind=TypeKind.LIST, params=(inner,))
        if base_name in ("dict", "Dict"):
            key_t, val_t = _typeref_pair_from_slice(slice_node, declared_classes)
            return TypeRef(kind=TypeKind.DICT, params=(key_t, val_t))
        if base_name in ("tuple", "Tuple"):
            params, variadic = _tuple_params_from_slice(slice_node, declared_classes)
            return TypeRef(kind=TypeKind.TUPLE, params=tuple(params), tuple_variadic=variadic)
        if base_name == "Optional":
            inner = _annotation_to_typeref(slice_node, declared_classes)
            return TypeRef(kind=TypeKind.OPTIONAL, params=(inner,))
        if base_name == "Union":
            params = _union_params_from_slice(slice_node, declared_classes)
            if params:
                return TypeRef(kind=TypeKind.UNION, params=tuple(params))
        # Unknown subscripted generic (e.g. Callable[..., int]) — unresolved.
        return TypeRef.unknown(f"{base_name}[...]")

    # X | Y | None syntax (PEP 604)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        parts = _flatten_bitor(node, declared_classes)
        # Detect Optional pattern: X | None → Optional[X]
        non_none = [p for p in parts if p.kind is not TypeKind.NONE]
        if len(non_none) == 1 and len(parts) > 1:
            return TypeRef(kind=TypeKind.OPTIONAL, params=(non_none[0],))
        return TypeRef(kind=TypeKind.UNION, params=tuple(parts))

    # Attribute chain (e.g. typing.Optional) — we don't resolve.
    return TypeRef.unknown(ast.unparse(node) if hasattr(ast, "unparse") else "<complex>")


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _typeref_pair_from_slice(
    slice_node: ast.AST, declared: set[str]
) -> tuple[TypeRef, TypeRef]:
    # Python 3.9+ ast: slice is ast.Tuple with elts=[k, v]
    if isinstance(slice_node, ast.Tuple) and len(slice_node.elts) == 2:
        k = _annotation_to_typeref(slice_node.elts[0], declared)
        v = _annotation_to_typeref(slice_node.elts[1], declared)
        return (k, v)
    # Malformed — return unknowns so comparator treats as unresolved.
    return (TypeRef.unknown("<malformed dict key>"), TypeRef.unknown("<malformed dict val>"))


def _tuple_params_from_slice(
    slice_node: ast.AST, declared: set[str]
) -> tuple[list[TypeRef], bool]:
    # tuple[X, ...] → variadic
    if isinstance(slice_node, ast.Tuple):
        elts = slice_node.elts
        # Detect trailing Ellipsis → variadic
        if elts and isinstance(elts[-1], ast.Constant) and elts[-1].value is Ellipsis:
            core = [_annotation_to_typeref(e, declared) for e in elts[:-1]]
            return (core, True)
        return ([_annotation_to_typeref(e, declared) for e in elts], False)
    # Single-param tuple: tuple[X]
    single = _annotation_to_typeref(slice_node, declared)
    return ([single], False)


def _union_params_from_slice(
    slice_node: ast.AST, declared: set[str]
) -> list[TypeRef]:
    if isinstance(slice_node, ast.Tuple):
        return [_annotation_to_typeref(e, declared) for e in slice_node.elts]
    return [_annotation_to_typeref(slice_node, declared)]


def _flatten_bitor(node: ast.AST, declared: set[str]) -> list[TypeRef]:
    """Flatten a chain of BitOr ops into a flat list of TypeRef."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _flatten_bitor(node.left, declared) + _flatten_bitor(
            node.right, declared
        )
    return [_annotation_to_typeref(node, declared)]


# ============================================================
# L2 extractor — test body AST → usage traces
# ============================================================


@dataclass(frozen=True)
class AccessStep:
    """One step in a value-access chain.

    Kinds:
      - ``subscript_index`` — ``x[42]`` or ``x[-1]`` (numeric key)
      - ``subscript_str`` — ``x['key']`` (string key)
      - ``subscript_var`` — ``x[name_or_expr]`` (can't statically tell)
      - ``attribute`` — ``x.attr``
      - ``call`` — ``x(...)`` (rare but possible via callables)
    """

    kind: str
    value: str = ""  # literal value, attribute name, or descriptive placeholder


@dataclass(frozen=True)
class UsageTrace:
    """A method call captured from a test body, plus the chain of
    indexing/attribute accesses applied to its return value.

    Example: ``shortener.list_mappings()[0]['hash']`` →
      ``class_name="URLShortener", method_name="list_mappings",
        steps=[subscript_index(0), subscript_str("hash")]``

    ``test_name`` and ``body_line`` help error messages point at the
    offending test function + line within the body.

    ``consumed_call_ids`` holds the ``id(ast.Call)`` values of inner
    Call nodes that contributed to this trace's step chain (via
    ``obj.method1().method2()``). The extractor uses it to avoid
    emitting duplicate standalone traces for the inner calls.
    """

    test_name: str
    class_name: str
    method_name: str
    steps: tuple[AccessStep, ...] = ()
    body_line: int = 0
    consumed_call_ids: frozenset[int] = field(default_factory=frozenset)


def extract_usage_traces(
    test_source: str, contract: ContractModel
) -> list[UsageTrace]:
    """Walk a test file's AST; collect usage traces for every call whose
    receiver resolves to a spec-declared class.

    Only catches the patterns that actually appear in LLM-authored tests:
    direct constructor chain (``URLShortener().method()``), simple
    same-scope aliases (``x = URLShortener(); x.method()``), and class-
    method-style calls (``URLShortener.method(...)``). Anything more
    indirect (variable passed through functions, conditional assignment,
    list comprehensions over instances) is intentionally skipped —
    unresolved ≠ violation, per permissive-by-default.
    """
    try:
        tree = ast.parse(textwrap.dedent(test_source))
    except SyntaxError:
        return []

    class_names = contract.all_class_names()
    traces: list[UsageTrace] = []

    def _walk_test_fn(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Alias map: var_name → class_name for ``x = URLShortener(...)``
        # style constructor assignments. Lets ``x.method()`` be resolved.
        aliases: dict[str, str] = {}
        # Return-alias map: var_name → (class_name, method_name) for
        # ``x = shortener.method()`` style assignments. Lets us follow
        # downstream access on ``x`` as if it were an extension of the
        # method's return chain.
        return_aliases: dict[str, tuple[str, str]] = {}
        # Comprehension iteration aliases: iter_var → (class_name,
        # method_name, element_extraction=True). Represents ``for m in
        # <return-aliased-var>`` where m is an element of the return value.
        iter_aliases: dict[str, tuple[str, str]] = {}

        for node in ast.walk(func_node):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                cls = _call_resolves_to_class(node.value, class_names)
                if cls is not None:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            aliases[target.id] = cls
                    continue
                # `x = receiver.method()` where receiver resolves to a class:
                # record the return alias so subsequent `x[...]` chains
                # produce traces rooted at the method.
                call_trace = _describe_method_call(
                    node.value, class_names, aliases
                )
                if call_trace is not None:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            return_aliases[target.id] = call_trace

        # Walk comprehensions + for-stmts: any ``for m in x`` where x is
        # a return-aliased variable OR a direct method call on a spec
        # class treats m as an element of the iterable's return value.
        for node in ast.walk(func_node):
            iter_node = None
            target = None
            if isinstance(node, ast.comprehension) or isinstance(node, (ast.For, ast.AsyncFor)):
                iter_node = node.iter
                target = node.target
            else:
                continue
            if not isinstance(target, ast.Name):
                continue
            cls_meth: tuple[str, str] | None = None
            if isinstance(iter_node, ast.Name) and iter_node.id in return_aliases:
                cls_meth = return_aliases[iter_node.id]
            elif isinstance(iter_node, ast.Call):
                # Direct `for m in shortener.method()` — no intermediate var.
                cls_meth = _describe_method_call(iter_node, class_names, aliases)
            if cls_meth is not None:
                iter_aliases[target.id] = cls_meth

        # Parent map for outward walking.
        parents = _build_parent_map(func_node)

        # Track calls we've already consumed as STEPS of an outer trace
        # so we don't also emit them as standalone traces.
        consumed: set[int] = set()

        for node in ast.walk(func_node):
            if not isinstance(node, ast.Call):
                continue
            if id(node) in consumed:
                continue
            trace = _try_build_trace(
                node, func_node.name, class_names, aliases, parents
            )
            if trace is None:
                continue
            traces.append(trace)
            for step_id in trace.consumed_call_ids:
                consumed.add(step_id)

        # Emit traces for `return_aliased_var`-rooted accesses:
        # e.g. `mappings[0]['hash']` where `mappings` was the return of
        # `shortener.list_mappings()`. The trace is structurally
        # equivalent to `shortener.list_mappings()[0]['hash']`.
        _emit_traces_from_name_accesses(
            func_node,
            return_aliases,
            iter_aliases,
            class_names,
            parents,
            traces,
        )

    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if top.name.startswith("test_"):
                _walk_test_fn(top)
        elif isinstance(top, ast.ClassDef) and top.name.startswith("Test"):
            for sub in top.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if sub.name.startswith("test_"):
                        _walk_test_fn(sub)
    return traces


def _build_parent_map(root: ast.AST) -> dict[int, ast.AST]:
    """Map each child's ``id(node)`` to its parent. One pass, O(nodes)."""
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(root):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _call_resolves_to_class(call: ast.Call, class_names: set[str]) -> str | None:
    """If ``Call`` is a constructor invocation of a declared class,
    return that class name; else None."""
    func = call.func
    if isinstance(func, ast.Name) and func.id in class_names:
        return func.id
    return None


def _try_build_trace(
    call: ast.Call,
    test_name: str,
    class_names: set[str],
    aliases: dict[str, str],
    parents: dict[int, ast.AST],
) -> UsageTrace | None:
    """Convert a method-call node into a UsageTrace when the receiver
    resolves to a declared class; else return None.

    Walks outward from ``call`` via the parent map to collect every
    subscript / attribute / call step applied to the return value.
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    method_name = func.attr
    receiver = func.value
    resolved_class: str | None = None
    if isinstance(receiver, ast.Name):
        if receiver.id in aliases:
            resolved_class = aliases[receiver.id]
        elif receiver.id in class_names:
            resolved_class = receiver.id
    elif isinstance(receiver, ast.Call):
        resolved_class = _call_resolves_to_class(receiver, class_names)
    if resolved_class is None:
        return None

    steps, consumed = _walk_outward(call, parents)
    return UsageTrace(
        test_name=test_name,
        class_name=resolved_class,
        method_name=method_name,
        steps=tuple(steps),
        body_line=getattr(call, "lineno", 0),
        consumed_call_ids=frozenset(consumed),
    )


def _walk_outward(
    call: ast.Call, parents: dict[int, ast.AST]
) -> tuple[list[AccessStep], list[int]]:
    """From ``call``, walk outward through wrapping Subscript / Attribute /
    Call nodes. Return the access-step chain + any inner Call node ids
    we consumed as steps (so the outer caller doesn't also emit a
    standalone trace for those)."""
    steps: list[AccessStep] = []
    consumed: list[int] = []
    current: ast.AST = call
    while True:
        parent = parents.get(id(current))
        if parent is None:
            break
        if isinstance(parent, ast.Subscript) and parent.value is current:
            steps.append(_subscript_to_step(parent.slice))
            current = parent
            continue
        if isinstance(parent, ast.Attribute) and parent.value is current:
            steps.append(AccessStep(kind="attribute", value=parent.attr))
            current = parent
            continue
        if isinstance(parent, ast.Call) and parent.func is current:
            steps.append(AccessStep(kind="call"))
            consumed.append(id(parent))
            current = parent
            continue
        break
    return steps, consumed


def _describe_method_call(
    call: ast.Call,
    class_names: set[str],
    aliases: dict[str, str],
) -> tuple[str, str] | None:
    """If ``call`` is ``<receiver>.method(...)`` where receiver resolves
    to a declared class, return ``(class_name, method_name)``; else None.

    Used to register return-aliases: ``x = receiver.method()``.
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    receiver = func.value
    cls: str | None = None
    if isinstance(receiver, ast.Name):
        cls = aliases.get(receiver.id) or (
            receiver.id if receiver.id in class_names else None
        )
    elif isinstance(receiver, ast.Call):
        cls = _call_resolves_to_class(receiver, class_names)
    if cls is None:
        return None
    return (cls, func.attr)


def _emit_traces_from_name_accesses(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    return_aliases: dict[str, tuple[str, str]],
    iter_aliases: dict[str, tuple[str, str]],
    class_names: set[str],
    parents: dict[int, ast.AST],
    out: list[UsageTrace],
) -> None:
    """For each Name node referring to a return-aliased variable or
    iter-aliased comprehension target, walk outward to build a UsageTrace.

    The trace is rooted at the method that originally produced the
    return value; a synthetic "iterator" step marks the iter_aliases
    case so the L1↔L2 diff can treat it as "extract element".
    """
    reported: set[int] = set()
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Name):
            continue
        if id(node) in reported:
            continue
        # Only consider Load contexts (reads); writes are Store/Del.
        if not isinstance(node.ctx, ast.Load):
            continue
        cls_meth: tuple[str, str] | None = None
        is_iter_alias = False
        if node.id in return_aliases:
            cls_meth = return_aliases[node.id]
        elif node.id in iter_aliases:
            cls_meth = iter_aliases[node.id]
            is_iter_alias = True
        if cls_meth is None:
            continue

        steps, consumed = _walk_outward_from_name(node, parents)
        if not steps and not is_iter_alias:
            # Bare name use with no access steps — not a trace worth reporting.
            continue
        # For iter-aliased names, prepend an "iter" step so the diff
        # knows to extract an element from the method's list/tuple
        # return before applying downstream steps.
        if is_iter_alias:
            steps = [AccessStep(kind="iter")] + steps

        trace = UsageTrace(
            test_name=func_node.name,
            class_name=cls_meth[0],
            method_name=cls_meth[1],
            steps=tuple(steps),
            body_line=getattr(node, "lineno", 0),
            consumed_call_ids=frozenset(consumed),
        )
        out.append(trace)
        reported.add(id(node))


def _walk_outward_from_name(
    name_node: ast.Name, parents: dict[int, ast.AST]
) -> tuple[list[AccessStep], list[int]]:
    """Same as :func:`_walk_outward` but seeded at a Name node."""
    steps: list[AccessStep] = []
    consumed: list[int] = []
    current: ast.AST = name_node
    while True:
        parent = parents.get(id(current))
        if parent is None:
            break
        if isinstance(parent, ast.Subscript) and parent.value is current:
            steps.append(_subscript_to_step(parent.slice))
            current = parent
            continue
        if isinstance(parent, ast.Attribute) and parent.value is current:
            steps.append(AccessStep(kind="attribute", value=parent.attr))
            current = parent
            continue
        if isinstance(parent, ast.Call) and parent.func is current:
            steps.append(AccessStep(kind="call"))
            consumed.append(id(parent))
            current = parent
            continue
        break
    return steps, consumed


def _subscript_to_step(slice_node: ast.AST) -> AccessStep:
    """Classify a subscript slice as numeric / string / unknown."""
    if isinstance(slice_node, ast.Constant):
        val = slice_node.value
        if isinstance(val, int):
            return AccessStep(kind="subscript_index", value=str(val))
        if isinstance(val, str):
            return AccessStep(kind="subscript_str", value=val)
    if isinstance(slice_node, ast.UnaryOp) and isinstance(slice_node.op, ast.USub):
        # `-1` style negative index
        operand = slice_node.operand
        if isinstance(operand, ast.Constant) and isinstance(operand.value, int):
            return AccessStep(kind="subscript_index", value=str(-operand.value))
    # Slice like x[a:b], Name, etc. — unresolved.
    return AccessStep(kind="subscript_var", value="<expr>")


# ============================================================
# L1 ↔ L2 diff — tester's usage must be consistent with spec's return types
# ============================================================


# A curated allow-list of attribute/method names on primitive types.
# When we see ``str_method.upper()``, we don't want to flag that as a
# violation. These are the common stdlib operations we'll always allow.
_STR_METHODS = frozenset({
    "upper", "lower", "strip", "lstrip", "rstrip", "split", "rsplit",
    "join", "replace", "startswith", "endswith", "find", "rfind",
    "format", "encode", "title", "capitalize", "casefold", "index",
    "count", "isalpha", "isdigit", "isalnum", "isascii", "isspace",
    "zfill", "partition", "rpartition", "splitlines", "translate",
    "maketrans", "center", "ljust", "rjust", "removeprefix", "removesuffix",
})


def check_usage_matches_contract(
    traces: list[UsageTrace], contract: ContractModel
) -> list[Violation]:
    """Return all drift violations between test-body usage and the spec.

    Permissive/strict filtering is the CALLER's job — we return every
    finding, tagged with severity. Callers apply :func:`filter_by_mode`.
    """
    violations: list[Violation] = []
    for trace in traces:
        cls_lookup = contract.find_class(trace.class_name)
        if cls_lookup is None:
            # Shouldn't happen — tracer only emits known classes — but
            # be defensive anyway.
            continue
        _mod, cls_model = cls_lookup
        method = cls_model.find_method(trace.method_name)
        if method is None:
            # Unknown method; caught by find_unknown_method_calls elsewhere.
            # Don't double-report here.
            continue
        if not trace.steps:
            continue  # just calling the method, no return-value access
        current_type = method.return_type
        path_parts: list[str] = [
            f"{trace.test_name} → {trace.class_name}.{trace.method_name}()"
        ]
        for step in trace.steps:
            next_type, violation = _step_against_type(
                current_type, step, path_parts, contract
            )
            if violation is not None:
                violations.append(violation)
                # Stop walking further steps — downstream types are
                # derived from this; reporting cascading errors adds noise.
                break
            current_type = next_type
            path_parts.append(_format_step_for_path(step))
    return violations


def _step_against_type(
    current: TypeRef,
    step: AccessStep,
    path_parts: list[str],
    contract: ContractModel,
) -> tuple[TypeRef, Violation | None]:
    """Apply ``step`` to ``current`` type. Return (result_type, violation).

    Violation is None when the step is compatible (or the types are
    unresolved). Otherwise, a ``certain`` violation with a descriptive
    message. ``unresolved`` violations are emitted when we KNOW a
    piece is unresolved AND the caller asked to be strict — caller
    filters at end.
    """
    path = " → ".join(path_parts + [_format_step_for_path(step)])

    # Any / Unknown / Union with Unknown: can't tell — return Unknown, no violation.
    # (Note: strict mode can see this by checking severity="unresolved"
    # violations; we emit one here for visibility.)
    if current.kind in (TypeKind.UNKNOWN, TypeKind.ANY):
        return (
            TypeRef.unknown(),
            Violation(
                path=path,
                message=(
                    f"receiver has unresolved type "
                    f"({current.describe()}); can't verify step"
                ),
                severity="unresolved",
            ),
        )

    # Optional[X]: apply step to X, but note the possibility of None at
    # runtime. We permit the step against X and leave None-handling to
    # runtime asserts.
    if current.kind is TypeKind.OPTIONAL:
        inner = current.params[0] if current.params else TypeRef.unknown()
        return _step_against_type(inner, step, path_parts, contract)

    if current.kind is TypeKind.UNION:
        # For union, the step must be compatible with ALL members. If
        # any member is unknown, treat as unresolved.
        if any(p.kind in (TypeKind.UNKNOWN, TypeKind.ANY) for p in current.params):
            return (
                TypeRef.unknown(),
                Violation(
                    path=path,
                    message=(
                        f"receiver has union type with unresolved member "
                        f"({current.describe()}); can't verify step"
                    ),
                    severity="unresolved",
                ),
            )
        # Check every member; if any says violation, it's a violation.
        result_candidates: list[TypeRef] = []
        for member in current.params:
            t, v = _step_against_type(member, step, path_parts, contract)
            if v is not None and v.severity == "certain":
                return (TypeRef.unknown(), v)
            result_candidates.append(t)
        # All members accepted. Result is a union of the step outcomes,
        # but we don't re-collapse here — return Unknown to keep the
        # downstream check conservative. Losing precision here is fine.
        return (TypeRef.unknown(), None)

    # NONE: any step is invalid.
    if current.kind is TypeKind.NONE:
        return (
            TypeRef.unknown(),
            Violation(
                path=path,
                message=(
                    f"cannot apply {_format_step_for_path(step)} to None — "
                    f"the method's return type is None (no value to access)"
                ),
                severity="certain",
            ),
        )

    # PRIMITIVE
    if current.kind is TypeKind.PRIMITIVE:
        return _step_against_primitive(current, step, path)

    # LIST
    if current.kind is TypeKind.LIST:
        inner = current.params[0] if current.params else TypeRef.unknown()
        return _step_against_list(inner, step, path)

    # TUPLE
    if current.kind is TypeKind.TUPLE:
        return _step_against_tuple(current, step, path)

    # DICT
    if current.kind is TypeKind.DICT:
        k = current.params[0] if current.params else TypeRef.unknown()
        v = current.params[1] if len(current.params) > 1 else TypeRef.unknown()
        return _step_against_dict(k, v, step, path)

    # CLASS
    if current.kind is TypeKind.CLASS:
        return _step_against_class(current.name, step, path, contract)

    # Fallthrough (shouldn't reach here)
    return (TypeRef.unknown(), None)


def _step_against_primitive(
    current: TypeRef, step: AccessStep, path: str
) -> tuple[TypeRef, Violation | None]:
    if current.name == "str":
        if step.kind == "attribute":
            if step.value in _STR_METHODS:
                # Common str method — result is roughly str, though
                # several return other types (len → int). Conservatively
                # return Unknown so downstream steps are permissive.
                return (TypeRef.unknown(), None)
            # Private / dunder / unknown attribute — treat as unresolved
            # rather than certain violation (there are lots of legit str
            # methods we haven't listed).
            return (
                TypeRef.unknown(),
                Violation(
                    path=path,
                    message=(
                        f"unknown attribute .{step.value} on str; "
                        f"verify it's a standard string method"
                    ),
                    severity="unresolved",
                ),
            )
        if step.kind in ("subscript_index", "subscript_var"):
            # str[int] → str. Fine.
            return (TypeRef.primitive("str"), None)
        if step.kind == "subscript_str":
            # str['key'] is a TypeError at runtime.
            return (
                TypeRef.unknown(),
                Violation(
                    path=path,
                    message=(
                        f"subscripting str with a string key ({step.value!r}) "
                        f"is invalid — strings only accept int indices. "
                        f"Did you expect a dict here?"
                    ),
                    severity="certain",
                ),
            )
        if step.kind == "call":
            return (
                TypeRef.unknown(),
                Violation(
                    path=path,
                    message=(
                        "calling a str value as a function is invalid"
                    ),
                    severity="certain",
                ),
            )
    # int/bool/float: subscripts + attribute access are almost always wrong.
    if current.name in ("int", "bool", "float"):
        if step.kind in ("subscript_index", "subscript_str", "subscript_var"):
            return (
                TypeRef.unknown(),
                Violation(
                    path=path,
                    message=(
                        f"{current.name} has no subscript support — "
                        f"you can't index a {current.name} value"
                    ),
                    severity="certain",
                ),
            )
        if step.kind == "call":
            return (
                TypeRef.unknown(),
                Violation(
                    path=path,
                    message=f"cannot call a {current.name} value",
                    severity="certain",
                ),
            )
        # attribute access on numerics — rarely meaningful. Treat as
        # unresolved (e.g. numpy has `.real`, but we can't model that).
        return (TypeRef.unknown(), None)
    # Other primitives: bytes, complex — permissive.
    return (TypeRef.unknown(), None)


def _step_against_list(
    element: TypeRef, step: AccessStep, path: str
) -> tuple[TypeRef, Violation | None]:
    if step.kind == "iter":
        # `for x in list[X]` → x is X. Same as indexing for our purposes.
        return (element, None)
    if step.kind in ("subscript_index", "subscript_var"):
        return (element, None)
    if step.kind == "subscript_str":
        return (
            TypeRef.unknown(),
            Violation(
                path=path,
                message=(
                    f"subscripting a list with a string key ({step.value!r}) "
                    f"is invalid — lists take integer indices. If you "
                    f"expected a dict, the spec's return type "
                    f"(list[{element.describe()}]) says otherwise; "
                    f"either fix the test OR update the spec."
                ),
                severity="certain",
            ),
        )
    if step.kind == "attribute":
        # Common list methods (.append, .extend, .index, etc.) — permit.
        # Anything else → unresolved.
        return (TypeRef.unknown(), None)
    if step.kind == "call":
        return (
            TypeRef.unknown(),
            Violation(
                path=path,
                message="cannot call a list value",
                severity="certain",
            ),
        )
    return (TypeRef.unknown(), None)


def _step_against_tuple(
    current: TypeRef, step: AccessStep, path: str
) -> tuple[TypeRef, Violation | None]:
    if step.kind == "iter":
        # Iterating a fixed tuple: each element has a potentially different
        # type. Conservative: return Unknown.
        if current.tuple_variadic and current.params:
            return (current.params[0], None)
        return (TypeRef.unknown(), None)
    if step.kind == "subscript_str":
        return (
            TypeRef.unknown(),
            Violation(
                path=path,
                message=(
                    f"subscripting a tuple with a string key ({step.value!r}) "
                    f"is invalid — tuples take integer indices"
                ),
                severity="certain",
            ),
        )
    if step.kind == "subscript_index":
        idx = int(step.value)
        if current.tuple_variadic:
            # tuple[X, ...] → any index returns X (the single parameter)
            return (
                current.params[0] if current.params else TypeRef.unknown(),
                None,
            )
        # Fixed-arity tuple: index must be in range.
        n = len(current.params)
        if n == 0:
            return (TypeRef.unknown(), None)  # empty tuple annotation — permit
        # Negative indices wrap from the end.
        effective = idx if idx >= 0 else n + idx
        if 0 <= effective < n:
            return (current.params[effective], None)
        return (
            TypeRef.unknown(),
            Violation(
                path=path,
                message=(
                    f"tuple index {idx} out of bounds — "
                    f"the spec declares tuple of arity {n}"
                ),
                severity="certain",
            ),
        )
    if step.kind == "subscript_var":
        # Variable-index on a fixed tuple could be in range — unresolved.
        return (TypeRef.unknown(), None)
    if step.kind == "attribute":
        return (TypeRef.unknown(), None)
    if step.kind == "call":
        return (
            TypeRef.unknown(),
            Violation(
                path=path,
                message="cannot call a tuple value",
                severity="certain",
            ),
        )
    return (TypeRef.unknown(), None)


def _step_against_dict(
    key_t: TypeRef, val_t: TypeRef, step: AccessStep, path: str
) -> tuple[TypeRef, Violation | None]:
    if step.kind == "subscript_index":
        # dict[str, V][0] only valid if key is int
        if key_t.kind is TypeKind.PRIMITIVE and key_t.name == "int":
            return (val_t, None)
        if key_t.kind is TypeKind.PRIMITIVE and key_t.name == "str":
            return (
                TypeRef.unknown(),
                Violation(
                    path=path,
                    message=(
                        f"subscripting dict[str, {val_t.describe()}] with "
                        f"integer index {step.value} is invalid — use a "
                        f"string key"
                    ),
                    severity="certain",
                ),
            )
        # Key type unknown — unresolved.
        return (val_t, None)
    if step.kind == "subscript_str":
        if key_t.kind is TypeKind.PRIMITIVE and key_t.name == "str":
            return (val_t, None)
        # Mismatch
        if key_t.kind is TypeKind.PRIMITIVE and key_t.name != "str":
            return (
                TypeRef.unknown(),
                Violation(
                    path=path,
                    message=(
                        f"subscripting dict[{key_t.describe()}, ...] with "
                        f"string key is invalid — keys are {key_t.describe()}"
                    ),
                    severity="certain",
                ),
            )
        return (val_t, None)
    if step.kind == "subscript_var":
        return (val_t, None)
    if step.kind == "attribute":
        # dict methods: .items, .keys, .values, .get, .pop, etc.
        # Permissive — don't model individually.
        return (TypeRef.unknown(), None)
    if step.kind == "call":
        return (
            TypeRef.unknown(),
            Violation(
                path=path,
                message="cannot call a dict value",
                severity="certain",
            ),
        )
    return (TypeRef.unknown(), None)


def _step_against_class(
    class_name: str, step: AccessStep, path: str, contract: ContractModel
) -> tuple[TypeRef, Violation | None]:
    if step.kind == "attribute":
        # Could be a method (call follows) or an attribute. We don't model
        # class attributes (no info in spec), so permit.
        # If next step is a call, this is a method invocation — allow.
        # Either way, unresolved return type.
        return (TypeRef.unknown(), None)
    if step.kind in ("subscript_index", "subscript_str", "subscript_var"):
        # Subscripting a class instance requires __getitem__. Uncommon
        # for api_spec classes. Flag as unresolved (not certain — some
        # classes legitimately do this).
        return (
            TypeRef.unknown(),
            Violation(
                path=path,
                message=(
                    f"subscripting an instance of {class_name} — the class "
                    f"must implement __getitem__; unusual for api_spec "
                    f"classes"
                ),
                severity="unresolved",
            ),
        )
    if step.kind == "call":
        return (TypeRef.unknown(), None)
    return (TypeRef.unknown(), None)


def _format_step_for_path(step: AccessStep) -> str:
    """Render an AccessStep for display in error-path breadcrumbs."""
    if step.kind == "subscript_index":
        return f"[{step.value}]"
    if step.kind == "subscript_str":
        return f"[{step.value!r}]"
    if step.kind == "subscript_var":
        return "[<expr>]"
    if step.kind == "attribute":
        return f".{step.value}"
    if step.kind == "call":
        return "(...)"
    if step.kind == "iter":
        return " (iter-element)"
    return f"<{step.kind}>"


# ============================================================
# L3 extractor + self-consistency diff — catches field-name typos
# ============================================================


@dataclass(frozen=True)
class ImplClassModel:
    """What a class in ``src/*.py`` actually exposes, derived from AST.

    Used by :func:`check_impl_self_consistent` to detect bugs like
    "``__init__`` sets ``self.url_mapping`` but ``add_url`` reads
    ``self.url_hash_map``" — a typo that pytest only catches at
    runtime on first method call.
    """

    name: str
    method_names: tuple[str, ...] = ()
    attrs_set: tuple[str, ...] = ()
    attrs_read: tuple[str, ...] = ()


def extract_impl_classes(source: str) -> list[ImplClassModel]:
    """Parse impl source; return an :class:`ImplClassModel` per class.

    For each class:
      - ``method_names`` = every ``def foo`` at class body level
      - ``attrs_set`` = every ``self.X = ...`` (or aug-assignment) across
        all methods
      - ``attrs_read`` = every ``self.X`` read outside assignment target
        context
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[ImplClassModel] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        method_names: list[str] = []
        attrs_set: set[str] = set()
        attrs_read: set[str] = set()
        for sub in node.body:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_names.append(sub.name)
                for walked in ast.walk(sub):
                    if isinstance(walked, ast.Attribute) and _is_self_attr(walked):
                        if isinstance(walked.ctx, ast.Store):
                            attrs_set.add(walked.attr)
                        elif isinstance(walked.ctx, ast.Load):
                            attrs_read.add(walked.attr)
                        # Del context: treat as write-side (the attribute
                        # existed before) — add to both.
                        elif isinstance(walked.ctx, ast.Del):
                            attrs_set.add(walked.attr)
                    # AugAssign `self.x += 1` has attribute in Store context
                    # at the target but also reads the pre-value — treat
                    # as both.
                for walked in ast.walk(sub):
                    if isinstance(walked, ast.AugAssign):
                        target = walked.target
                        if isinstance(target, ast.Attribute) and _is_self_attr(target):
                            attrs_set.add(target.attr)
                            attrs_read.add(target.attr)
        out.append(
            ImplClassModel(
                name=node.name,
                method_names=tuple(method_names),
                attrs_set=tuple(sorted(attrs_set)),
                attrs_read=tuple(sorted(attrs_read)),
            )
        )
    return out


def _is_self_attr(node: ast.Attribute) -> bool:
    """True if node is ``self.X`` (not ``other.X``)."""
    return isinstance(node.value, ast.Name) and node.value.id == "self"


def check_impl_self_consistent(
    impl_classes: list[ImplClassModel],
) -> list[Violation]:
    """Return a violation for every attribute the class READS without ever
    SETTING.

    Note: methods declared on the class (``method_names``) are excluded
    from ``attrs_read`` evaluation — calling ``self.method()`` doesn't
    count as reading an attribute that needs to be assigned. Private
    helpers (``self._foo``) are treated the same as public ones.

    Does NOT flag reads of methods, @property-style computed attributes
    we can't statically detect, or attributes set via ``setattr``.
    Permissive-leaning on purpose — the false-positive floor is higher
    than the return-type checker.
    """
    violations: list[Violation] = []
    for cls in impl_classes:
        methods = set(cls.method_names)
        unset = [
            attr for attr in cls.attrs_read
            if attr not in cls.attrs_set and attr not in methods
        ]
        if not unset:
            continue
        preview = ", ".join(sorted(unset)[:5])
        more = f" (+{len(unset) - 5} more)" if len(unset) > 5 else ""
        set_preview = ", ".join(sorted(cls.attrs_set)[:5]) or "(none)"
        violations.append(
            Violation(
                path=f"class {cls.name}",
                message=(
                    f"reads attribute(s) that were never set: "
                    f"{preview}{more}. Known self.* assignments in this "
                    f"class: {set_preview}. This is almost always a typo "
                    f"— e.g. __init__ sets self.url_mapping but a method "
                    f"reads self.url_hash_map. Unify the name."
                ),
                severity="certain",
            )
        )
    return violations


__all__ = [
    "AccessStep",
    "ClassModel",
    "ContractModel",
    "ImplClassModel",
    "MethodSignature",
    "Mode",
    "ModuleModel",
    "Severity",
    "TypeKind",
    "TypeRef",
    "UsageTrace",
    "Violation",
    "check_impl_self_consistent",
    "check_usage_matches_contract",
    "extract_contract",
    "extract_impl_classes",
    "extract_usage_traces",
    "filter_by_mode",
]
