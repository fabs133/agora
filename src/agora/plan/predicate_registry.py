"""Named-predicate registry: the bridge between YAML plan references and runtime Predicates.

YAML plans reference postconditions by ``name`` + ``args``. The registry maps
``name → factory(**args) → Predicate``. Factories must accept only JSON-
serializable kwargs — that constraint is what makes plans round-trippable.

The registered names and the predicate names the factories produce are kept
byte-for-byte identical to the inline patterns used in the runner scripts
(``_postcond_file_exists`` etc.) so ``Specification.fingerprint`` hashes match
for structural round-trip tests.

All six factories from :mod:`agora.fleet.runtime_postconditions` are registered
eagerly at import, plus the four previously-inline helpers that are lifted out
of the runner scripts into this module as public factories.
"""

from __future__ import annotations

import ast
import py_compile
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agora.core.contract import Predicate, make_predicate
from agora.core.errors import AgoraError


Factory = Callable[..., Predicate]
_REGISTRY: dict[str, Factory] = {}


def register_predicate(name: str) -> Callable[[Factory], Factory]:
    """Decorator: register ``factory`` under ``name``.

    Factories must accept only JSON-serializable kwargs (``str``, ``int``,
    ``float``, ``bool``, ``list``, ``dict``). Callers should not register from
    within tests at import time — see ``tests/plan/conftest.py`` for the cleanup
    pattern if a test needs to add a predicate temporarily.
    """

    def decorator(factory: Factory) -> Factory:
        if name in _REGISTRY:
            raise AgoraError(f"predicate {name!r} already registered")
        _REGISTRY[name] = factory
        return factory

    return decorator


def build_predicate(name: str, args: dict[str, Any] | None = None) -> Predicate:
    """Construct a :class:`Predicate` by registered name + kwargs."""
    if name not in _REGISTRY:
        raise AgoraError(
            f"unknown predicate {name!r}; registered: {sorted(_REGISTRY)}"
        )
    factory = _REGISTRY[name]
    return factory(**(args or {}))


def list_registered_predicates() -> list[str]:
    """Sorted list of registered predicate names (useful for generating docs + planner KB)."""
    return sorted(_REGISTRY)


def describe_registered_predicates() -> list[dict[str, Any]]:
    """Return a structured catalog of every registered predicate.

    Each entry: ``{name, args: [{name, type, default?, required}]}``. The
    plan-authoring planner writes this to ``plan/kb/postcondition_catalog.md``
    so the LLM knows which predicate names + argument shapes are valid
    without having to read Python source.
    """
    import inspect

    catalog: list[dict[str, Any]] = []
    for name in sorted(_REGISTRY):
        factory = _REGISTRY[name]
        sig = inspect.signature(factory)
        args: list[dict[str, Any]] = []
        for pname, param in sig.parameters.items():
            entry: dict[str, Any] = {
                "name": pname,
                "type": _friendly_type_name(param.annotation),
                "required": param.default is inspect.Parameter.empty,
            }
            if param.default is not inspect.Parameter.empty:
                entry["default"] = param.default
            args.append(entry)
        catalog.append({"name": name, "args": args})
    return catalog


def _friendly_type_name(annotation: Any) -> str:
    """Return a short readable type-name for an annotation. Best-effort."""
    import inspect as _inspect

    if annotation is _inspect.Parameter.empty:
        return "any"
    if annotation is str:
        return "str"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is bool:
        return "bool"
    # Generic types (list[str] etc.) — str() is usually readable.
    return str(annotation).replace("typing.", "")


def _require(name: str, check) -> Predicate:
    """Mirror the inline ``_require`` helper used by every runner script.

    ``make_predicate(name, description=name, evaluate=check)`` — description
    equals name so the fingerprint is stable and short.
    """
    return make_predicate(name, name, check)


# --------------------------------------------------------------------- lifted helpers
# These four predicate factories were previously defined privately inside every
# runner script (_postcond_file_exists, _postcond_file_contains, etc.) and
# duplicated across three scripts. Lifting them into the framework both
# de-duplicates and makes them addressable from YAML plans.


@register_predicate("file_exists")
def postcond_file_exists(rel: str) -> Predicate:
    """An artifact path containing ``rel`` must be recorded by the task."""

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        artifacts = ctx.get("artifacts") or []
        return (
            any(rel in a for a in artifacts),
            f"expected a recorded artifact containing {rel!r}",
        )

    # No [:60] truncation — matches run_fastapi_crud_test.py:86 verbatim so
    # Specification.fingerprint is stable across Python-authored vs. YAML plans.
    return _require(f"artifact_contains_{rel.replace('/', '_')}", check)


@register_predicate("mark_complete")
def postcond_mark_complete() -> Predicate:
    """The task must have called ``mark_complete`` (directly or auto-synthesized)."""

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        return (bool(ctx.get("completions")), "mark_complete was not called")

    return _require("mark_complete_called", check)


@register_predicate("file_contains")
def postcond_file_contains(rel: str, substring: str) -> Predicate:
    """``rel`` (under work_dir) must contain the literal ``substring``."""

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        path = Path(work_dir) / rel
        if not path.is_file():
            return (False, f"{rel} does not exist under work_dir")
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return (False, f"could not read {rel}: {exc}")
        return (substring in body, f"{rel} does not contain {substring!r}")

    # Name-generation matches the inline runner pattern verbatim so
    # Specification.fingerprint is stable across Python-authored vs. YAML plans.
    safe_sub = substring.replace(" ", "_").replace(".", "_")
    return _require(f"{rel.replace('/', '_')}_has_{safe_sub}"[:60], check)


@register_predicate("py_compiles")
def postcond_py_compiles(rel: str) -> Predicate:
    """``rel`` must parse via ``py_compile`` AND have no module-scope undefined names."""

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        # Imported here so the framework doesn't pull inner_tools at module load time.
        from agora.fleet.inner_tools import _find_module_scope_undefined_names

        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        path = Path(work_dir) / rel
        if not path.is_file():
            return (False, f"{rel} does not exist under work_dir")
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            return (False, f"{rel} failed py_compile: {str(exc.msg).strip()[:200]}")
        except SyntaxError as exc:
            return (False, f"{rel} SyntaxError at line {exc.lineno}: {exc.msg}")
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            return (False, f"could not read {rel}: {exc}")
        undefined = _find_module_scope_undefined_names(source)
        if undefined:
            preview = ", ".join(f"{n}@L{ln}" for n, ln in undefined[:5])
            return (False, f"{rel} has undefined module-scope name(s): {preview}")
        return (True, "")

    # Name matches the inline runner pattern used in run_fastapi_crud_test.py:143
    # (``/``→``_`` only; ``.`` NOT replaced). Keeping this verbatim is what lets
    # hand-authored plan YAML + the fastapi-crud runner share
    # Specification.fingerprint for the round-trip test. The discord-bot runners
    # use a different local helper that also replaces ``.``; that's isolated to
    # those scripts and unaffected here.
    return _require(f"{rel.replace('/', '_')}_py_compiles"[:60], check)


# --------------------------------------------------------------------- framework factories
# Thin delegations to the existing factories in runtime_postconditions — just
# gives them short stable names addressable from YAML.


@register_predicate("python_imports")
def _python_imports(rel: str, timeout: float = 15.0) -> Predicate:
    from agora.fleet.runtime_postconditions import postcond_python_imports

    return postcond_python_imports(rel, timeout=timeout)


@register_predicate("pytest_passes")
def _pytest_passes(rel: str = ".", timeout: float = 60.0) -> Predicate:
    from agora.fleet.runtime_postconditions import postcond_pytest_passes

    return postcond_pytest_passes(rel, timeout=timeout)


@register_predicate("requirements_parse")
def _requirements_parse(rel: str = "requirements.txt") -> Predicate:
    from agora.fleet.runtime_postconditions import postcond_requirements_parse

    return postcond_requirements_parse(rel)


@register_predicate("bot_calls_tree_sync")
def _bot_calls_tree_sync(rel: str = "bot.py") -> Predicate:
    from agora.fleet.runtime_postconditions import postcond_bot_calls_tree_sync

    return postcond_bot_calls_tree_sync(rel)


@register_predicate("readme_commands_exist")
def _readme_commands_exist(
    readme: str = "README.md", bot: str = "bot.py"
) -> Predicate:
    from agora.fleet.runtime_postconditions import (
        postcond_readme_only_references_existing_commands,
    )

    return postcond_readme_only_references_existing_commands(readme=readme, bot=bot)


@register_predicate("no_code_after_main_block")
def _no_code_after_main_block(rel: str) -> Predicate:
    from agora.fleet.runtime_postconditions import postcond_no_code_after_main_block

    return postcond_no_code_after_main_block(rel)


# --------------------------------------------------------------------- plan-level check


@register_predicate("max_line_length")
def postcond_max_line_length(rel: str, max_chars: int = 120) -> Predicate:
    """Every non-blank line of ``rel`` (under work_dir) must be ≤ ``max_chars``.

    Plan-builder complexity gate: enforces that task descriptions in
    ``plan/tasks.md`` stay compact. On failure the in-phase auto-retry
    injects the failure reason as a learning ("split long tasks").
    """

    _rel = rel
    _max = int(max_chars)

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        path = Path(work_dir) / _rel
        if not path.is_file():
            return (False, f"{_rel} does not exist under work_dir")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return (False, f"could not read {_rel}: {exc}")
        over: list[tuple[int, int]] = []
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.rstrip()
            if not stripped:
                continue
            if len(stripped) > _max:
                over.append((i, len(stripped)))
        if over:
            preview = ", ".join(f"L{ln}={n}ch" for ln, n in over[:3])
            return (
                False,
                f"{_rel} has {len(over)} line(s) exceeding {_max} chars — split them: {preview}",
            )
        return (True, "")

    return _require(f"{_rel.replace('/', '_').replace('.', '_')}_lines_le_{_max}"[:60], check)


@register_predicate("no_task_exceeds_complexity")
def postcond_no_task_exceeds_complexity(
    plan_path: str,
    max_chars: int = 3000,
    max_stages: int = 3,
    max_postconditions: int = 8,
) -> Predicate:
    """Plan-level gate: no single task in ``plan_path`` exceeds the budget.

    Loads the YAML at ``plan_path`` (relative to work_dir), inspects every
    task, and fails if any task's combined instruction length, stage count, or
    postcondition count exceeds the thresholds. Used by the plan-builder's
    ``assemble_plan`` task to force decomposition via in-phase auto-retry.
    """

    _rel = plan_path
    _max_chars = int(max_chars)
    _max_stages = int(max_stages)
    _max_pcs = int(max_postconditions)

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        import yaml as _yaml

        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        path = Path(work_dir) / _rel
        if not path.is_file():
            return (False, f"{_rel} does not exist under work_dir")
        try:
            data = _yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, _yaml.YAMLError) as exc:
            return (False, f"could not parse {_rel} as YAML: {exc}")
        if not isinstance(data, dict):
            return (False, f"{_rel} YAML root must be a mapping")
        tasks = data.get("task_graph") or []
        violations: list[str] = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = t.get("id", "?")
            desc = str(t.get("description", ""))
            stages = t.get("stages") or []
            instruction_chars = len(desc) + sum(
                len(str(s.get("instruction", ""))) for s in stages if isinstance(s, dict)
            )
            pcs = t.get("postconditions") or []
            if instruction_chars > _max_chars:
                violations.append(
                    f"task {tid!r} has {instruction_chars} chars (> {_max_chars})"
                )
            if len(stages) > _max_stages:
                violations.append(
                    f"task {tid!r} has {len(stages)} stages (> {_max_stages})"
                )
            if len(pcs) > _max_pcs:
                violations.append(
                    f"task {tid!r} has {len(pcs)} postconditions (> {_max_pcs})"
                )
        if violations:
            preview = "; ".join(violations[:3])
            return (
                False,
                f"{_rel} contains oversized tasks — split them: {preview}",
            )
        return (True, "")

    return _require(
        f"{_rel.replace('/', '_').replace('.', '_')}_complexity_ok"[:60], check
    )


# --------------------------------------------------- test-body content gate
# Sprint 7.1: the scaffolder emits one ``pytest.skip(...)`` stub per deliverable.
# The LLM stage is supposed to REPLACE each skip with real assertions. When
# the model writes a parseable file but leaves every body as a bare skip call,
# pytest exits 0 trivially and the contract/impl postconditions all pass with
# zero signal. ``tests_have_assertions`` is the gate that forces the tester
# to actually author test content.


def _test_function_is_placeholder(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A ``test_*`` function is a placeholder if every statement in its body
    is either a string-literal docstring or a ``pytest.skip(...)`` call.

    We accept extra skip-adjacent noop statements (``pass``) since linters
    sometimes add them, but the presence of any ``assert``, loop, exception
    handler, function/class definition, or non-skip call means the test
    has real content.
    """
    for node in func.body:
        if isinstance(node, ast.Expr):
            val = node.value
            # Plain string literal (docstring) — noop.
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                continue
            # ``pytest.skip(...)`` / ``skip(...)`` at statement level — noop.
            if isinstance(val, ast.Call):
                func_node = val.func
                name = ""
                if isinstance(func_node, ast.Attribute):
                    name = func_node.attr
                elif isinstance(func_node, ast.Name):
                    name = func_node.id
                if name in {"skip", "xfail"}:
                    continue
            # Any other Expr (e.g. `some_call()`) counts as real content.
            return False
        if isinstance(node, ast.Pass):
            continue
        # Anything else: assert / if / for / try / with / return / raise /
        # assignment / def / class → real content.
        return False
    return True


@register_predicate("tests_have_assertions")
def postcond_tests_have_assertions(rel: str) -> Predicate:
    """``rel`` (under work_dir) must be a test file where NO ``test_*``
    function is a pure ``pytest.skip`` placeholder.

    Enforces that the tester's LLM stage actually authored assertions. The
    scaffold emits ``def test_x(): pytest.skip("TODO: ...")`` stubs — this
    predicate fails until every stub is replaced with real test content.

    Fails if:
      - file missing / unparseable
      - no ``test_*`` functions defined at all
      - one or more ``test_*`` functions still contain only docstring + skip
    """

    _rel = rel

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        path = Path(work_dir) / _rel
        if not path.is_file():
            return (False, f"{_rel} does not exist under work_dir")
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            return (False, f"could not read {_rel}: {exc}")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return (False, f"{_rel} SyntaxError at line {exc.lineno}: {exc.msg}")

        test_funcs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        # Walk top-level AND one level of classes so class-based tests count.
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    test_funcs.append(node)
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if sub.name.startswith("test_"):
                            test_funcs.append(sub)

        if not test_funcs:
            return (False, f"{_rel} has no test_* functions defined")

        placeholders = [
            fn.name for fn in test_funcs if _test_function_is_placeholder(fn)
        ]
        if placeholders:
            preview = ", ".join(placeholders[:3])
            more = f" (+{len(placeholders) - 3} more)" if len(placeholders) > 3 else ""
            return (
                False,
                f"{_rel}: {len(placeholders)}/{len(test_funcs)} test(s) still "
                f"pytest.skip placeholders — replace skip with real assertions: "
                f"{preview}{more}",
            )
        return (True, "")

    return _require(f"{_rel.replace('/', '_').replace('.', '_')}_has_assertions"[:60], check)


# --------------------------------------------------- api_spec validity gate
# Sprint 7.5: the plan-builder's ``define_api`` task writes plan/api_spec.md
# which becomes the SHARED API truth for tester + implementer. A malformed
# spec (e.g. top-level functions with ``self`` as first param, or zero
# module sections) propagates silently and breaks every downstream task.
# This gate fails the define_api task early with a specific reason so the
# architect's retry sees structural feedback.


#: Brief-deliverable verb dictionary — maps natural-language action words a
#: user puts in a ``## Key deliverables`` bullet to the method-name tokens the
#: api_spec is expected to contain. Matching is substring, case-insensitive,
#: across every method+function name across every module in the spec.
#:
#: Keys are lowercase match-verbs; values are the set of tokens that satisfy
#: the bullet. A bullet matches if ANY of its extracted verbs has ≥1 token
#: substring-matching an api_spec method/function name. Bullets with no
#: match against any key are considered "no actionable verb" and skipped
#: (they pass vacuously — see _extract_verbs).
#:
#: Keep this conservative — prefer false-negatives over false-positives. A
#: missing verb means the gate doesn't fire on a legitimate case; a bogus
#: keyword means the architect can sneak past the gate without real coverage.
_BRIEF_VERB_KEYWORDS: dict[str, tuple[str, ...]] = {
    "add": ("add", "append", "insert", "create", "put", "new", "register"),
    "insert": ("add", "insert", "append", "put"),
    "create": ("add", "create", "new", "make", "build"),
    "lookup": ("lookup", "look_up", "get", "find", "fetch", "retrieve", "resolve"),
    "look": ("lookup", "look_up", "get", "find", "fetch", "retrieve"),
    "get": ("get", "fetch", "retrieve", "lookup", "find", "read"),
    "find": ("find", "lookup", "get", "search"),
    "retrieve": ("retrieve", "get", "fetch", "lookup"),
    "search": ("search", "find", "query"),
    "list": ("list", "all", "mappings", "items", "entries", "iter"),
    "show": ("show", "list", "display", "print"),
    "display": ("show", "display", "print", "list"),
    "persist": ("save", "persist", "store", "dump", "load", "write", "read"),
    "save": ("save", "persist", "store", "dump", "write"),
    "store": ("save", "store", "persist", "dump", "write"),
    "load": ("load", "read", "restore"),
    "read": ("read", "load", "restore"),
    "write": ("write", "save", "persist", "dump"),
    "disk": ("save", "load", "persist", "store", "dump", "write", "read", "file"),
    "file": ("save", "load", "write", "read", "file"),
    "delete": ("delete", "remove", "clear", "drop", "pop"),
    "remove": ("remove", "delete", "clear", "drop", "pop"),
    "clear": ("clear", "reset", "remove", "drop"),
    "update": ("update", "set", "edit", "modify", "patch"),
    "modify": ("modify", "update", "edit", "set"),
    "collision": ("collision", "unique", "dedupe", "hash", "conflict"),
    "unique": ("unique", "dedupe", "hash", "collision"),
    "validate": ("validate", "check", "verify"),
    "count": ("count", "len", "size", "total"),
}


def _extract_verbs(bullet: str) -> list[str]:
    """Lowercase-tokenize ``bullet`` and return the verb keywords that match
    :data:`_BRIEF_VERB_KEYWORDS`. Deduped, preserving first-seen order.

    A bullet with no verb match returns an empty list — the caller treats
    that as "no actionable verb" and skips coverage enforcement.
    """
    import re as _re

    tokens = [t.lower() for t in _re.findall(r"[a-zA-Z_]+", bullet or "")]
    seen: set[str] = set()
    verbs: list[str] = []
    # Two-token lookahead for "look up" → single verb "look".
    for i, tok in enumerate(tokens):
        if tok in _BRIEF_VERB_KEYWORDS and tok not in seen:
            verbs.append(tok)
            seen.add(tok)
    return verbs


def _extract_brief_bullets(brief_text: str) -> list[str]:
    """Return the ``## Key deliverables`` bullets from a brief markdown file.

    Recognises lines starting with ``-`` or ``*`` under the deliverables
    heading. Stops at the next ``##`` section header or EOF. Case-insensitive
    on the section name, but requires the exact phrase "Key deliverables"
    (plural, capital-K in source, but match is case-insensitive).
    """
    if not brief_text:
        return []
    lines = brief_text.splitlines()
    bullets: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        # Header line?
        if stripped.startswith("##"):
            hdr = stripped.lstrip("#").strip().lower()
            in_section = "key deliverables" in hdr
            continue
        if not in_section:
            continue
        if stripped.startswith(("-", "*", "+")):
            text = stripped.lstrip("-*+ ").strip()
            if text:
                bullets.append(text)
    return bullets


@register_predicate("api_spec_covers_brief_deliverables")
def postcond_api_spec_covers_brief_deliverables(
    rel: str = "plan/api_spec.md",
    brief_rel: str = "plan/brief.md",
) -> Predicate:
    """``rel`` must contain at least one method/function matching each brief
    deliverable's action verb.

    Gate the ``define_api`` task with this to catch the common 7B failure
    where the architect writes an api_spec covering only the first 2-3
    bullets of the brief and silently drops the rest ("Persist to disk"
    disappearing when the model forgets, observed in live runs).

    Matching strategy:
      - Parse ``brief_rel`` for ``## Key deliverables`` bullets.
      - For each bullet, extract verb tokens via :func:`_extract_verbs`.
      - If a bullet has zero actionable verbs (prose-only, unusual),
        it passes vacuously — we can't guess what method it would need.
      - For each actionable verb, require ≥1 api_spec method/function
        name to contain one of :data:`_BRIEF_VERB_KEYWORDS[verb]` as a
        substring (case-insensitive, ``_``-tolerant).
      - On failure, return a structured message naming the bullet,
        the missing verb, the expected keywords, and the known symbols.

    ``rel`` / ``brief_rel`` are resolved relative to ``ctx["work_dir"]``.
    """

    _rel = rel
    _brief_rel = brief_rel

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        from agora.plan.api_spec import parse_api_spec

        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        spec_path = Path(work_dir) / _rel
        brief_path = Path(work_dir) / _brief_rel
        if not spec_path.is_file():
            return (False, f"{_rel} does not exist under work_dir")
        if not brief_path.is_file():
            return (False, f"{_brief_rel} does not exist under work_dir")
        try:
            spec_text = spec_path.read_text(encoding="utf-8", errors="replace")
            brief_text = brief_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return (False, f"could not read spec or brief: {exc}")

        bullets = _extract_brief_bullets(brief_text)
        if not bullets:
            # No ``## Key deliverables`` section → nothing to enforce.
            return (True, "")

        modules = parse_api_spec(spec_text)
        symbols: list[str] = []
        for module in modules:
            for cls in module.classes:
                for method in cls.methods:
                    symbols.append(method.name)
            for fn in module.functions:
                symbols.append(fn.name)
        # Normalise once: lowercase + strip leading/trailing ``_``.
        symbols_lc = [s.lower() for s in symbols]

        missing: list[str] = []
        for bullet in bullets:
            verbs = _extract_verbs(bullet)
            if not verbs:
                # Prose-only bullet; can't validate. Pass.
                continue
            for verb in verbs:
                keywords = _BRIEF_VERB_KEYWORDS.get(verb, ())
                if not keywords:
                    continue
                matched = any(
                    any(kw in sym for kw in keywords) for sym in symbols_lc
                )
                if matched:
                    # First verb match wins — bullet is covered.
                    break
            else:
                # No verb in this bullet matched any api_spec symbol.
                bullet_short = bullet if len(bullet) <= 60 else bullet[:57] + "…"
                kw_set: set[str] = set()
                for v in verbs:
                    kw_set.update(_BRIEF_VERB_KEYWORDS.get(v, ()))
                missing.append(
                    f"'{bullet_short}' — no method matches any of "
                    f"{sorted(kw_set)[:8]}"
                )

        if missing:
            known = sorted(set(symbols))
            known_preview = known[:10] + (["…"] if len(known) > 10 else [])
            lines = [
                f"{_rel} missing coverage for brief deliverables:",
            ]
            for item in missing[:5]:
                lines.append(f"  - {item}")
            if len(missing) > 5:
                lines.append(f"  (+{len(missing) - 5} more)")
            lines.append(
                f"known api_spec symbols: {known_preview}"
            )
            lines.append(
                "fix: add the missing method(s) to a module in "
                f"{_rel}, e.g. 'def save(path: str) -> None: ...'"
            )
            return (False, "\n".join(lines))
        return (True, "")

    return _require(
        f"{_rel.replace('/', '_').replace('.', '_')}_covers_brief"[:60], check
    )


@register_predicate("api_spec_is_valid")
def postcond_api_spec_is_valid(rel: str = "plan/api_spec.md") -> Predicate:
    """``rel`` (under work_dir) must parse as a valid API spec.

    Validation:
      - File exists under work_dir
      - At least ONE ``## module: src/<path>.py`` header
      - Each declared module body parses as valid Python (v2.9 / C5:
        silently-dropped bodies now surface here instead of disappearing)
      - NO duplicate ``## module:`` paths (v2.9 / C5: catches the
        concatenated-spec failure where the architect re-authored the
        same module twice; seed_workspace's first-wins policy then
        silently dropped the second write)
      - NO duplicate class or top-level function names within a single
        module body (v2.9 / C5: same root cause — authoring-time
        confusion producing a file the scaffolder can't sensibly stub)
      - NO top-level function has ``self`` as first parameter (that means
        the architect meant to write a method and forgot the enclosing
        class — breaks every test that imports the function)
      - At least ONE class OR one non-self function across all modules
        (empty specs don't help)
    """

    _rel = rel

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        from agora.plan.api_spec import extract_declared_modules, parse_api_spec

        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        path = Path(work_dir) / _rel
        if not path.is_file():
            return (False, f"{_rel} does not exist under work_dir")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return (False, f"could not read {_rel}: {exc}")

        # v2.9 (C5): consult the full declared-module list so we can catch
        # sections parse_api_spec silently drops (e.g. bodies with stray
        # markdown, mixed prose, placeholder tokens). Without this the
        # architect can ship a spec that "looks valid" but is missing
        # whole modules downstream — observed 2026-04-22 with a
        # concatenated 3-section spec where sections 3-4 failed to parse.
        declared = extract_declared_modules(text)
        if not declared:
            return (
                False,
                f"{_rel} has no `## module: src/<path>.py` sections — "
                "add at least one module header.",
            )

        problems: list[str] = []

        # Surface silently-dropped sections (parser returned None).
        for dm in declared:
            if dm.is_valid:
                continue
            problems.append(
                f"module `{dm.path}` at line {dm.header_line} failed to "
                f"parse — {dm.parse_error}. "
                f"Remove any non-Python content (markdown lists, stray "
                f"prose, placeholder tokens like <pkg>) from the body "
                f"and keep only `def`/`class` signatures with `...` bodies."
            )

        # Duplicate module paths (first-wins policy downstream hides
        # subsequent sections — reject so the architect re-authors a
        # single coherent version).
        seen_paths: dict[str, int] = {}
        for dm in declared:
            norm = dm.path.replace("\\", "/")
            if norm in seen_paths:
                problems.append(
                    f"duplicate `## module: {dm.path}` headers at lines "
                    f"{seen_paths[norm]} and {dm.header_line} — merge the "
                    f"two sections into ONE block with a single class/"
                    f"function list. Downstream scaffolders keep only the "
                    f"first, silently dropping later content."
                )
            else:
                seen_paths[norm] = dm.header_line

        # Duplicate symbol names within a module body (parser accepts
        # them but they produce a stub with two `class X:` defs, which
        # makes edit tools ambiguous and contract tests nondeterministic).
        modules = parse_api_spec(text)
        for module in modules:
            class_names = [c.name for c in module.classes]
            fn_names = [f.name for f in module.functions]
            dup_classes = _dups(class_names)
            dup_fns = _dups(fn_names)
            if dup_classes:
                problems.append(
                    f"{module.path}: class name(s) defined more than once: "
                    f"{sorted(dup_classes)}. Merge method lists under a "
                    f"single class definition."
                )
            if dup_fns:
                problems.append(
                    f"{module.path}: function name(s) defined more than "
                    f"once: {sorted(dup_fns)}. Keep one definition per "
                    f"symbol per module."
                )

        total_symbols = 0
        for module in modules:
            # Reject test-path modules. The api_spec is for PRODUCTION code;
            # test files (tests/, src/tests/) are scaffolded separately by
            # the test-authoring pipeline (Sprint 7.2-7.3).
            norm = module.path.replace("\\", "/")
            if norm.startswith("tests/") or norm.startswith("src/tests/"):
                problems.append(
                    f"{module.path} is a TEST module — the api_spec is only "
                    f"for production src/*.py modules. Remove this section."
                )
                continue
            if not norm.startswith("src/"):
                problems.append(
                    f"{module.path} is outside src/ — the api_spec is only "
                    f"for production modules under src/."
                )
                continue
            # Validate top-level functions: no `self` first param.
            for fn in module.functions:
                first_arg = _first_arg_name(fn.source)
                if first_arg == "self":
                    problems.append(
                        f"{module.path}::{fn.name} is a TOP-LEVEL function "
                        f"with `self` as first parameter — put it inside a "
                        f"class, OR drop the `self` param"
                    )
                else:
                    total_symbols += 1
            total_symbols += len(module.classes)
        if problems:
            # Show up to 5 problems so the architect sees a complete
            # enough picture to re-author in ONE retry (not one-at-a-time).
            shown = problems[:5]
            body = "\n  - ".join(shown)
            more = (
                f"\n  (+{len(problems) - 5} more)"
                if len(problems) > 5
                else ""
            )
            return (False, f"{_rel}:\n  - {body}{more}")
        if total_symbols == 0:
            return (
                False,
                f"{_rel} has module headers but zero class/function "
                "signatures — add at least one public symbol per module.",
            )
        return (True, "")

    return _require(
        f"{_rel.replace('/', '_').replace('.', '_')}_is_valid"[:60], check
    )


def _dups(names: list[str]) -> set[str]:
    """Return the set of names that appear more than once in ``names``."""
    seen: set[str] = set()
    dupes: set[str] = set()
    for n in names:
        if n in seen:
            dupes.add(n)
        else:
            seen.add(n)
    return dupes


@register_predicate("class_attributes_consistent")
def postcond_class_attributes_consistent(rel: str) -> Predicate:
    """v2.9 Phase 3 gate: every ``self.X`` read must have a matching
    ``self.X = ...`` somewhere in the same class.

    Catches the 2026-04-22 failure where ``__init__`` set
    ``self.url_mapping`` but every method then referenced the typo'd
    ``self.url_hash_map``. pytest catches this at runtime; this
    predicate catches it at class-write time so the implementer's
    retry budget isn't burned on guaranteed-to-fail test runs.

    Permissive-by-default: only ``certain`` violations are flagged
    (reads with no matching set). Does NOT flag ``@property``-computed
    attributes, ``setattr``-dynamic attributes, or reads of declared
    methods — those are not statically inferrable.
    """

    _rel = rel

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        work_dir = ctx.get("work_dir")
        if not work_dir:
            return (False, "work_dir missing from postcondition context")
        path = Path(work_dir) / _rel
        if not path.is_file():
            return (False, f"{_rel} does not exist under work_dir")
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            return (False, f"could not read {_rel}: {exc}")

        from agora.plan.structure import (
            check_impl_self_consistent,
            extract_impl_classes,
        )

        classes = extract_impl_classes(source)
        if not classes:
            return (True, "")  # no classes — nothing to check

        violations = check_impl_self_consistent(classes)
        if not violations:
            return (True, "")
        lines = [f"{_rel}: class self-attribute inconsistencies:"]
        for v in violations[:3]:
            lines.append(f"  - {v.path}: {v.message}")
        if len(violations) > 3:
            lines.append(f"  (+{len(violations) - 3} more)")
        return (False, "\n".join(lines))

    return _require(
        f"{_rel.replace('/', '_').replace('.', '_')}_attrs_consistent"[:60],
        check,
    )


def _first_arg_name(func_source: str) -> str:
    """Extract the first positional arg name from a ``def`` source line/block."""
    import ast as _ast

    try:
        tree = _ast.parse(func_source)
    except SyntaxError:
        return ""
    if not tree.body:
        return ""
    fn = tree.body[0]
    if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
        return ""
    args = fn.args.args
    return args[0].arg if args else ""


# --------------------------------------------------- plan-draft gate predicates
# These inspect the mutable ``PlanDraft`` state attached to ``ToolContext`` to
# gate the author-stage tasks of the plan-builder. They let the planner's
# in-phase auto-retry fire on "not enough tasks added yet" etc. rather than
# silently emitting an empty plan.yaml.


@register_predicate("plan_draft_has_min_tasks")
def postcond_plan_draft_has_min_tasks(min_tasks: int = 4) -> Predicate:
    """Plan-builder gate: ``ctx.plan_draft`` must carry at least ``min_tasks``."""

    _min = int(min_tasks)

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        draft = ctx.get("plan_draft")
        if draft is None or not hasattr(draft, "tasks"):
            return (False, "no plan_draft on context")
        n = len(draft.tasks)
        if n < _min:
            return (False, f"plan_draft has {n} tasks, need ≥ {_min}")
        return (True, "")

    return _require(f"plan_draft_ge_{_min}_tasks"[:60], check)


@register_predicate("plan_draft_every_task_has_postcondition")
def postcond_plan_draft_every_task_has_postcondition() -> Predicate:
    """Plan-builder gate: every task in the draft must have ≥ 1 postcondition."""

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        draft = ctx.get("plan_draft")
        if draft is None or not hasattr(draft, "tasks"):
            return (False, "no plan_draft on context")
        bad = [tid for tid, t in draft.tasks.items() if not t.get("postconditions")]
        if bad:
            preview = ", ".join(bad[:3])
            more = f" (+{len(bad) - 3} more)" if len(bad) > 3 else ""
            return (False, f"tasks without postconditions: {preview}{more}")
        return (True, "")

    return _require("plan_draft_all_tasks_have_postcond", check)


@register_predicate("plan_draft_has_min_agents")
def postcond_plan_draft_has_min_agents(min_agents: int = 2) -> Predicate:
    """Plan-builder gate: draft must carry ≥ ``min_agents`` agents. Used as
    the ``author_agents`` task postcondition so in-phase retry engages if the
    narrow per-agent stages didn't populate enough slots."""

    _min = int(min_agents)

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        draft = ctx.get("plan_draft")
        if draft is None or not hasattr(draft, "agents"):
            return (False, "no plan_draft on context")
        n = len(draft.agents)
        if n < _min:
            return (False, f"plan_draft has {n} agents, need ≥ {_min}")
        return (True, "")

    return _require(f"plan_draft_ge_{_min}_agents"[:60], check)


@register_predicate("plan_draft_all_agents_valid")
def postcond_plan_draft_all_agents_valid() -> Predicate:
    """Plan-builder gate: every agent in the draft passes ``validate_agent``.

    Catches silently-bad agents (empty instructions, invalid role) that the
    per-stage ``plan_validate_agent`` check missed — e.g. an agent authored
    via a different surface or one whose instructions were truncated.
    """

    def check(ctx: dict[str, Any]) -> tuple[bool, str]:
        draft = ctx.get("plan_draft")
        if draft is None or not hasattr(draft, "agents"):
            return (False, "no plan_draft on context")
        failures: list[str] = []
        for a in draft.agents:
            name = a.get("name", "<unnamed>")
            try:
                problems = draft.validate_agent(name)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {exc}")
                continue
            if problems:
                failures.append(f"{name}: {problems[0]}")
        if failures:
            preview = "; ".join(failures[:3])
            more = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
            return (False, f"invalid agents: {preview}{more}")
        return (True, "")

    return _require("plan_draft_all_agents_valid", check)


# Sanity: the registry is read-only after this import. Re-registration in
# third-party code is permitted but discouraged (register_predicate raises on
# duplicate). Tests that add predicates must explicitly clean up.
_VALID_ARG_TYPES = (str, int, float, bool, list, dict, type(None))


def _validate_args_are_serializable(args: dict[str, Any]) -> None:
    """Defensive check caller-side — not invoked by the registry itself."""
    for key, value in args.items():
        if not isinstance(key, str):
            raise AgoraError(f"arg key must be str, got {type(key)!r}")
        if not isinstance(value, _VALID_ARG_TYPES):
            raise AgoraError(
                f"arg {key!r} must be JSON-serializable primitive; got {type(value)!r}"
            )


__all__ = [
    "build_predicate",
    "list_registered_predicates",
    "postcond_file_contains",
    "postcond_file_exists",
    "postcond_mark_complete",
    "postcond_no_task_exceeds_complexity",
    "postcond_py_compiles",
    "register_predicate",
]
