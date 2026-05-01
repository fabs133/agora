"""Parse the plan-builder's shared API spec into structured module specs.

Sprint 7.5 — the single source of truth for API surface. The plan-builder's
``define_api`` task writes ``plan/api_spec.md`` with one section per module:

    ## module: src/core_domain.py

    class URLShortener:
        def __init__(self) -> None: ...
        def shorten(self, url: str) -> str: ...

    def helper(x: int) -> int: ...

    ## module: src/persistence.py

    def save(obj: URLShortener, path: str) -> None: ...
    def load(path: str) -> URLShortener: ...

This module parses that format and hands a structured view to the test
scaffolder (for real imports) AND the impl-stub scaffolder (for matching
signature stubs). Both agents see the SAME API — the "tester hallucinates,
implementer disagrees" coordination failure becomes structurally
impossible.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass, field


@dataclass
class FunctionSpec:
    """Top-level function signature."""

    name: str
    source: str  # the full `def ...: ...` line(s), as parsed


@dataclass
class ClassSpec:
    """Class signature + its methods."""

    name: str
    methods: list[FunctionSpec] = field(default_factory=list)


@dataclass
class ModuleSpec:
    """A single module's API surface."""

    path: str  # e.g. "src/core_domain.py"
    classes: list[ClassSpec] = field(default_factory=list)
    functions: list[FunctionSpec] = field(default_factory=list)
    #: Full parsed python source (with stub bodies) — the scaffolders can
    #: re-emit this directly to create matching src/ stub files.
    source: str = ""

    @property
    def dotted(self) -> str:
        """Convert ``src/core_domain.py`` → ``src.core_domain`` for imports."""
        trimmed = self.path
        if trimmed.endswith(".py"):
            trimmed = trimmed[:-3]
        return trimmed.replace("/", ".").replace("\\", ".")

    @property
    def import_names(self) -> list[str]:
        """Names exported at module level (class names + function names)."""
        return [c.name for c in self.classes] + [f.name for f in self.functions]


_MODULE_HEADER_RE = re.compile(
    r"^\s*##\s*module:\s*(?P<path>[^\s]+)\s*$", re.IGNORECASE
)


def parse_api_spec(spec_text: str) -> list[ModuleSpec]:
    """Parse the markdown API spec into a list of :class:`ModuleSpec`.

    Each ``## module: <path>`` header starts a new module. The body until
    the next header (or EOF) is parsed as Python source — ``ast.parse``
    extracts classes + top-level functions. Method signatures inside
    classes are captured via ``ClassSpec.methods``.

    Returns empty list when no module sections are found (caller should
    fall back to free-form scaffolding). Unparseable module bodies are
    skipped with a best-effort partial parse — see
    :func:`extract_declared_modules` when callers need to know about
    silently-dropped sections (e.g. the ``api_spec_is_valid`` predicate).
    """
    if not spec_text:
        return []
    lines = spec_text.splitlines(keepends=False)
    modules: list[ModuleSpec] = []
    current_path: str | None = None
    current_body: list[str] = []

    def _flush() -> None:
        if current_path is None:
            return
        body = "\n".join(current_body).strip("\n")
        if not body:
            return
        mod = _parse_module_body(current_path, body)
        if mod is not None:
            modules.append(mod)

    for line in lines:
        m = _MODULE_HEADER_RE.match(line)
        if m:
            _flush()
            current_path = m.group("path").strip()
            current_body = []
            continue
        if current_path is not None:
            current_body.append(line)
    _flush()
    return modules


@dataclass
class DeclaredModule:
    """A ``## module:`` section as declared in the source spec text.

    Exposes the raw body + parse status so validators can catch the case
    where :func:`parse_api_spec` silently dropped a section because its
    body didn't parse as Python. Validators can then surface "section
    for path X at line N failed to parse: <reason>" instead of letting
    the broken section vanish from downstream consumers.
    """

    path: str
    header_line: int  # 1-based line number of the ``## module:`` header
    body: str
    parsed: ModuleSpec | None = None
    parse_error: str = ""

    @property
    def is_valid(self) -> bool:
        return self.parsed is not None


def extract_declared_modules(spec_text: str) -> list[DeclaredModule]:
    """Return ONE :class:`DeclaredModule` per ``## module:`` header.

    Unlike :func:`parse_api_spec` which filters out unparseable sections,
    this includes every section along with its parse status. Used by
    ``api_spec_is_valid`` to catch silently-dropped sections, duplicate
    module paths, and stray prose that failed to parse.
    """
    if not spec_text:
        return []
    lines = spec_text.splitlines(keepends=False)
    declared: list[DeclaredModule] = []
    current_path: str | None = None
    current_header_line = 0
    current_body: list[str] = []

    def _flush() -> None:
        if current_path is None:
            return
        body = "\n".join(current_body).strip("\n")
        entry = DeclaredModule(
            path=current_path,
            header_line=current_header_line,
            body=body,
        )
        if not body:
            entry.parse_error = "empty module body"
        else:
            cleaned = _strip_code_fences(body).strip("\n")
            if not cleaned:
                entry.parse_error = "module body empty after stripping code fences"
            else:
                dedented = textwrap.dedent(cleaned)
                try:
                    tree = ast.parse(dedented)
                except SyntaxError as exc:
                    entry.parse_error = (
                        f"SyntaxError at body line {exc.lineno}: "
                        f"{exc.msg}"
                    )
                else:
                    # v2.9 (C5 follow-up): even when ast.parse succeeds,
                    # reject bodies with non-signature top-level statements.
                    # Observed failure 2026-04-22 PM: the architect emitted
                    # markdown bullets ``- src/cli.py`` inside a module
                    # body, which Python parses as unary-minus-over-division
                    # expressions (``-(src / cli.py)``) and would silently
                    # poison the stub file. We also reject assignments, if/
                    # for/while blocks, and any other non-def construct —
                    # an api_spec is a PURE SIGNATURE DOCUMENT.
                    stray = _find_stray_top_level_statements(tree)
                    if stray:
                        preview = "; ".join(stray[:3])
                        more = (
                            f" (+{len(stray) - 3} more)"
                            if len(stray) > 3
                            else ""
                        )
                        entry.parse_error = (
                            f"body contains non-signature top-level "
                            f"statement(s) — api_spec must be pure "
                            f"class/function definitions with `...` "
                            f"bodies, no expressions, assignments, or "
                            f"markdown-like lines: {preview}{more}"
                        )
                    else:
                        # Reuse the canonical parser so
                        # DeclaredModule.parsed is structurally identical
                        # to what parse_api_spec would return.
                        entry.parsed = _parse_module_body(current_path, body)
                        if entry.parsed is None:
                            # Parse succeeded as raw Python but yielded no
                            # recognisable module contents — unusual, treat
                            # as a parse failure so the validator flags it.
                            entry.parse_error = (
                                "body parsed but contained no class or "
                                "function definitions"
                            )
        declared.append(entry)

    for idx, line in enumerate(lines):
        m = _MODULE_HEADER_RE.match(line)
        if m:
            _flush()
            current_path = m.group("path").strip()
            current_header_line = idx + 1  # 1-based
            current_body = []
            continue
        if current_path is not None:
            current_body.append(line)
    _flush()
    return declared


def _parse_module_body(path: str, body: str) -> ModuleSpec | None:
    """Parse a single module body (between ``## module:`` headers)."""
    # Drop any surrounding fences (``` ```) the LLM may have added.
    cleaned = _strip_code_fences(body).strip("\n")
    if not cleaned:
        return None
    dedented = textwrap.dedent(cleaned)
    try:
        tree = ast.parse(dedented)
    except SyntaxError:
        return None
    mod = ModuleSpec(path=path, source=dedented + ("\n" if not dedented.endswith("\n") else ""))
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            spec = ClassSpec(name=node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    spec.methods.append(
                        FunctionSpec(name=sub.name, source=ast.unparse(sub))
                    )
            mod.classes.append(spec)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mod.functions.append(
                FunctionSpec(name=node.name, source=ast.unparse(node))
            )
    return mod


_FENCE_RE = re.compile(r"^\s*```(?:python|py)?\s*$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    """Remove ```python / ``` fences the LLM may have added around blocks."""
    return _FENCE_RE.sub("", text)


def strip_test_module_sections(spec_text: str) -> tuple[str, list[str]]:
    """Remove any ``## module: tests/...`` or ``## module: src/tests/...``
    sections from an api_spec. Returns ``(cleaned_text, removed_paths)``.

    Test modules have no place in the api_spec — the test-authoring
    pipeline scaffolds them separately. Observed across runs
    (2026-04-22): GPT-4o-mini persistently adds a test module section
    even when the instruction and the C5 validator's retry feedback
    both say "remove it". Stripping at write time is always safe (the
    content is invalid if present) and saves the architect's retry
    budget.

    Deterministic line-level pass: preserves everything outside test
    sections verbatim, including blank lines and ordering.
    """
    if not spec_text:
        return spec_text, []
    lines = spec_text.splitlines(keepends=True)
    removed: list[str] = []
    out_lines: list[str] = []
    skipping = False
    for line in lines:
        m = _MODULE_HEADER_RE.match(line)
        if m:
            path = m.group("path").strip().replace("\\", "/")
            is_test = path.startswith("tests/") or path.startswith("src/tests/")
            if is_test:
                removed.append(path)
                skipping = True
                continue
            # Production module — stop skipping and emit the header.
            skipping = False
            out_lines.append(line)
            continue
        if skipping:
            continue
        out_lines.append(line)
    # Trim trailing blank lines we may have left after removing a
    # trailing test section.
    while out_lines and out_lines[-1].strip() == "":
        out_lines.pop()
    cleaned = "".join(out_lines)
    # Ensure file ends with exactly one newline if it had content.
    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned, removed


def _find_stray_top_level_statements(tree: ast.Module) -> list[str]:
    """Return a list of short human descriptions for any top-level statement
    that isn't a valid api_spec element.

    Accepted at module top level:
      - ``ClassDef`` / ``FunctionDef`` / ``AsyncFunctionDef`` — signatures
      - ``Import`` / ``ImportFrom`` — discouraged but structural
      - ``Expr(Constant(str))`` — module docstring

    Rejected (return non-empty list):
      - Any other expression statement (e.g. ``-src/cli.py`` parsed as
        a binary-op expression, which is what markdown bullets look like
        to ast.parse — observed 2026-04-22 PM)
      - Assignments, if/for/while/try blocks, etc.

    Used by :func:`extract_declared_modules` to surface sections whose
    bodies are syntactically Python but structurally not a signature list.
    """
    offending: list[str] = []
    for i, node in enumerate(tree.body):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            # Module docstring — allow at position 0 only, though we don't
            # strictly enforce position (makes the check simpler).
            continue
        # Construct a short descriptor pointing at the offender's line.
        line = getattr(node, "lineno", "?")
        kind = type(node).__name__
        offending.append(f"line {line} ({kind})")
    return offending


def render_impl_stub(module: ModuleSpec) -> str:
    """Render the src/ file stub from a :class:`ModuleSpec`.

    The stub contains the exact signatures from the spec + placeholder
    bodies that raise :class:`NotImplementedError`. The implementer's
    job is to replace each placeholder with a real body — the `add_class_method`
    and `add_function` tools (Sprint 7.4) upsert-by-name, so replacing a
    stub body is a single tool call per method/function.

    Why ``raise NotImplementedError`` instead of ``pass``:
      - Makes contract tests FAIL LOUDLY (not quietly pass) when the
        implementer hasn't replaced the stub yet.
      - Catches "implementer forgot a method" at test time, not runtime.
    """
    lines: list[str] = []
    lines.append('"""Implementation stubs — bodies filled in by implementer."""')
    lines.append("")
    # Sprint 7.7 follow-up: PEP 563 makes annotations lazy strings so the
    # model can use ``Optional[str]`` / ``list[tuple[str, str]]`` without
    # remembering module-level typing imports. Observed failure: implementer
    # wrote ``def lookup(self, h) -> Optional[str]`` without importing
    # ``Optional``, module failed to import at class-definition time,
    # contract tests failed, task retry OVERWROTE the successful methods
    # via ``add_class`` replace.
    lines.append("from __future__ import annotations")
    lines.append("")
    for cls in module.classes:
        lines.append(f"class {cls.name}:")
        if not cls.methods:
            lines.append("    pass")
        else:
            for method in cls.methods:
                # Extract the `def X(...):` header from the method source
                # and emit a NotImplementedError body.
                header = _extract_def_header(method.source)
                lines.append(textwrap.indent(header, "    "))
                lines.append("        raise NotImplementedError")
        lines.append("")
    for fn in module.functions:
        header = _extract_def_header(fn.source)
        lines.append(header)
        lines.append("    raise NotImplementedError")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _extract_def_header(source: str) -> str:
    """From ``def foo(x: int) -> int: ...`` return ``def foo(x: int) -> int:``.

    Works on single-line or multi-line def source. Uses AST to find the
    function's signature and drops the body.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Best-effort fallback — return source up to the first ':' after 'def'.
        first_line = source.splitlines()[0]
        return first_line if first_line.endswith(":") else first_line + ":"
    if not tree.body:
        return source.splitlines()[0]
    fn = tree.body[0]
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return source.splitlines()[0]
    prefix = "async def " if isinstance(fn, ast.AsyncFunctionDef) else "def "
    args = ast.unparse(fn.args)
    returns = f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
    return f"{prefix}{fn.name}({args}){returns}:"


def render_test_imports(modules: list[ModuleSpec]) -> list[str]:
    """Emit ``from src.X import Y, Z`` lines for every named symbol across
    every module in the spec. Used by the contract test scaffolder so the
    test file's imports WILL resolve against the generated src/ stubs."""
    lines: list[str] = []
    for module in modules:
        names = module.import_names
        if not names:
            continue
        joined = ", ".join(sorted(names))
        lines.append(f"from {module.dotted} import {joined}")
    return lines


def find_unknown_method_calls(
    source: str, modules: list[ModuleSpec]
) -> list[str]:
    """Walk ``source`` AST; return violations where code calls a method
    that doesn't exist on a spec class.

    Sprint 7.6(g) — stops the recurring coordination failure where the
    tester's test body calls ``URLShortener().add(url)`` when the spec
    declares ``add_url``. Framework validates method names at the tool
    boundary so the model can't silently invent them.

    Detection scope:
      - ``ClassName().method_name(...)`` — direct constructor chain.
      - ``var = ClassName(...); var.method_name(...)`` — single-assignment
        aliases.
      - ``ClassName.method_name(...)`` — class-method/staticmethod style.

    Ignored:
      - ``_name`` and ``__name__`` — private/dunder are implementation detail.
      - Attribute access on return values of unknown calls.
      - Any symbol not resolvable to a spec class (too noisy to flag).

    Returns a list of human-readable violation messages (empty = OK).
    """
    if not modules:
        return []
    # class_name → {method_names}
    spec: dict[str, set[str]] = {}
    for module in modules:
        for cls in module.classes:
            spec.setdefault(cls.name, set()).update(m.name for m in cls.methods)
    if not spec:
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # caller handles parse errors separately

    # Track variable → class_name aliases via direct `x = ClassName(...)`.
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name) and func.id in spec:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = func.id

    violations: list[str] = []
    seen: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        attr = node.attr
        if attr.startswith("_"):
            continue  # skip private + dunder
        value = node.value
        cls: str | None = None
        if isinstance(value, ast.Name):
            if value.id in aliases:
                cls = aliases[value.id]
            elif value.id in spec:
                cls = value.id  # ClassName.staticmethod style
        elif isinstance(value, ast.Call):
            f = value.func
            if isinstance(f, ast.Name) and f.id in spec:
                cls = f.id
        if cls is None:
            continue
        known = spec.get(cls, set())
        if attr not in known:
            key = (cls, attr)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                f"{cls}.{attr} is not a method in the api_spec "
                f"(known methods: {sorted(known) or ['<none>']})"
            )
    return violations


__all__ = [
    "ClassSpec",
    "DeclaredModule",
    "FunctionSpec",
    "ModuleSpec",
    "extract_declared_modules",
    "find_unknown_method_calls",
    "parse_api_spec",
    "render_impl_stub",
    "render_test_imports",
    "strip_test_module_sections",
]
