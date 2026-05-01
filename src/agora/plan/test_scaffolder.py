"""Pure helpers for the ``plan_scaffold_tests`` framework stage.

Generates a ``tests/test_<x>.py`` skeleton whose imports match the actual
project layout and whose function bodies are ``pytest.skip('TODO: ...')``
stubs — one per bullet in ``plan/brief.md``'s ``## Key deliverables``
section. The tester LLM then only has to replace each ``pytest.skip(...)``
with real assertions; it never has to reconstruct boilerplate or guess
at import paths.

All functions are side-effect-free except :func:`discover_modules` which
reads from the filesystem. Parsing / rendering are pure.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


def parse_deliverables(brief_text: str) -> list[str]:
    """Extract the bullets under ``## Key deliverables`` from ``brief.md``.

    Returns a list of stripped bullet strings (without ``- `` prefix).
    Returns an empty list if the section isn't found or has no bullets;
    callers should fall back to a single generic test stub in that case.
    """
    if not brief_text:
        return []
    # Find the ## Key deliverables header (case-insensitive).
    lines = brief_text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*##\s+key\s+deliverables\s*$", line, re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        return []
    bullets: list[str] = []
    seen: set[str] = set()
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        # Stop at the next heading.
        if stripped.startswith("#"):
            break
        m = re.match(r"^[-*]\s+(.+)$", stripped)
        if m:
            bullet = m.group(1).strip()
            # Case-insensitive + punctuation-insensitive dedupe — weak models
            # editing brief.md sometimes duplicate deliverable bullets during
            # an "enrich" pass. Duplicate bullets cause scaffolded stubs to
            # have identical pytest.skip(...) strings, breaking edit_file_replace
            # with "matches N times" errors downstream. Dedupe here so the
            # scaffolder is robust to a sloppy brief.
            key = re.sub(r"[^\w\s]", "", bullet.lower()).strip()
            if key in seen:
                continue
            seen.add(key)
            bullets.append(bullet)
    return bullets


#: Module-path patterns to skip during discovery. ``__init__`` is the package
#: marker (not importable as a module usually), tests belong to the tester's
#: own output, and dunder-prefixed files are internal.
_SKIP_FILE_STEMS = {"__init__", "conftest"}


def discover_modules(work_dir: Path) -> dict[str, list[str]]:
    """Walk ``<work_dir>/src/**`` for importable modules.

    Returns a mapping of ``dotted.module.path -> [public_top_level_names]``.
    Uses a lightweight AST parse so we never execute any of the discovered
    source. Skips ``__init__.py``, ``conftest.py``, test files, and modules
    whose stem starts with ``_`` (private).

    If the project uses a flat ``src/<modname>.py`` layout, dotted paths are
    rooted at the parent package (e.g. ``src/__init__.py`` present →
    ``<workdir_basename>.<modname>``). Otherwise modules appear as bare names.

    Returns an empty dict if ``src/`` doesn't exist.
    """
    src = Path(work_dir) / "src"
    if not src.is_dir():
        return {}

    # Detect the package root. If src/<pkg>/__init__.py exists, the project
    # uses the common ``src/<pkg>/...`` layout and modules are imported as
    # ``<pkg>.<...>``. If src/__init__.py exists, modules are ``src.<...>``.
    # Else modules are bare ``<stem>`` and up to the user to resolve.
    package_prefix = ""
    # Case 1: src/__init__.py — src is itself the package.
    if (src / "__init__.py").is_file():
        package_prefix = src.name  # "src"
    else:
        # Case 2: look for src/<pkg>/__init__.py.
        child_pkgs = [
            p for p in src.iterdir()
            if p.is_dir() and (p / "__init__.py").is_file()
        ]
        if len(child_pkgs) == 1:
            package_prefix = child_pkgs[0].name
            src = child_pkgs[0]  # drill into the package

    modules: dict[str, list[str]] = {}
    for py in src.rglob("*.py"):
        stem = py.stem
        if stem in _SKIP_FILE_STEMS or stem.startswith("_"):
            continue
        if stem.startswith("test_"):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        # Collect top-level public names (classes, functions, assignments).
        names: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    names.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        names.append(target.id)
        if not names:
            continue  # nothing public to import

        rel = py.relative_to(src).with_suffix("")
        parts = list(rel.parts)
        if package_prefix:
            dotted = ".".join([package_prefix] + parts)
        else:
            dotted = ".".join(parts)
        modules[dotted] = names
    return modules


def snake_from_deliverable(bullet: str) -> str:
    """Convert ``'Add a long URL and get a short 6-char hash back'`` to
    ``'test_add_long_url_and_get_short_hash_back'``.

    Keeps [a-z0-9], collapses whitespace to underscores, truncates to 50
    chars, ensures ``test_`` prefix, dedupes double underscores.
    """
    if not bullet:
        return "test_deliverable"
    s = bullet.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", "_", s).strip("_")
    s = re.sub(r"_+", "_", s)
    if not s.startswith("test_"):
        s = "test_" + s
    if len(s) > 50:
        s = s[:50].rstrip("_")
    return s or "test_deliverable"


def render_scaffold(
    modules: dict[str, list[str]],
    deliverables: list[str],
    *,
    header: str = "",
    mode: str = "impl",
    api_spec_imports: list[str] | None = None,
) -> str:
    """Emit the test file content as a string.

    ``modules`` is the output of :func:`discover_modules`. ``deliverables``
    is the list of bullets. ``header`` is an optional one-line docstring
    summarizing the project; empty → default.

    Each deliverable becomes a test function with a ``pytest.skip('TODO: ...')``
    body so the file runs green out of the box — the tester's LLM stage
    replaces each skip with real assertions.

    ``mode`` controls how imports are shaped:
      - ``"impl"`` (default, Sprint 5 behavior): module-level ``from pkg.x
        import name`` for every discovered module. Used when the test file
        is authored AFTER the implementer tasks have produced the source,
        so the imports always resolve at collection time.
      - ``"contract"``: NO module-level imports. Header doc notes that
        imports must be deferred INSIDE each test function body. Used by
        contract tests that run before implementer tasks — module-level
        imports would fail collection; deferred imports let the file load
        and each test FAIL loudly when the implementation is missing,
        which is the right signal for a contract gate.

    ``api_spec_imports`` (Sprint 7.5) — when provided, the scaffolder
    emits REAL module-level ``from src.X import Y`` lines from the shared
    API spec, regardless of ``mode``. This is safe in contract mode too
    because the ``seed_workspace`` step pre-wrote matching src/ stubs
    (with NotImplementedError bodies) before the scaffolder runs — the
    imports always resolve at collection time, and tests fail loudly when
    they INVOKE a stub (NotImplementedError), not at import time.
    """
    mode = mode if mode in ("contract", "impl") else "impl"
    use_spec_imports = bool(api_spec_imports)
    if header:
        docstring = header
    elif mode == "contract" and use_spec_imports:
        docstring = (
            "Contract tests — behavioral invariants derived from the brief. "
            "Imports come from plan/api_spec.md; the executor pre-wrote "
            "matching src/ stubs so imports resolve at collection. Stubs "
            "raise NotImplementedError — tests fail when invoking them "
            "until the implementer fills in bodies."
        )
    elif mode == "contract":
        docstring = (
            "Contract tests — behavioral invariants derived from the brief. "
            "Imports are deferred inside each test body so this file loads "
            "before the implementation exists; each test fails with "
            "ImportError until the implementer satisfies the contract."
        )
    else:
        docstring = "Tests covering the project brief's deliverables."
    lines: list[str] = [
        f'"""{docstring}"""',
        "",
        "import pytest",
    ]

    # Sprint 7.5: if the caller provided api_spec_imports, those are the
    # authoritative real module-level imports — use them regardless of mode.
    if use_spec_imports:
        lines.extend(sorted(api_spec_imports))
        lines.append("")
    elif mode == "impl":
        # Legacy path: synthesize imports from discovered src/ modules.
        import_lines: list[str] = []
        for module in sorted(modules):
            names = modules[module]
            if not names:
                continue
            # Prefer a single-line import if it fits; otherwise wrap.
            joined = ", ".join(sorted(names))
            line = f"from {module} import {joined}"
            if len(line) > 100:
                # Wrap with parens.
                joined_multi = ",\n    ".join(sorted(names))
                line = f"from {module} import (\n    {joined_multi},\n)"
            import_lines.append(line)
        if import_lines:
            lines.extend(import_lines)
            lines.append("")
        else:
            lines.append("")
    else:
        # Contract mode without api_spec — stubs must defer their imports
        # inside each test function (legacy Sprint 7.2-7.4 behavior).
        lines.append("")

    # One test function per deliverable, or a single generic test if none.
    if deliverables:
        used: set[str] = set()
        for bullet in deliverables:
            name = snake_from_deliverable(bullet)
            # Dedupe if two bullets snake to the same name.
            base = name
            i = 2
            while name in used:
                name = f"{base}_{i}"
                i += 1
            used.add(name)
            lines.extend(
                [
                    "",
                    f"def {name}():",
                    f'    """{bullet}"""',
                    f'    pytest.skip("TODO: {bullet}")',
                ]
            )
    else:
        lines.extend(
            [
                "",
                "def test_main_flow():",
                '    """Fallback test — brief had no parseable deliverables."""',
                '    pytest.skip("TODO: no deliverables parsed from brief.md")',
            ]
        )
    return "\n".join(lines) + "\n"


def replace_test_body(source: str, test_name: str, body_code: str) -> str:
    """Replace the body of ``def test_name()`` in ``source`` with ``body_code``.

    Pure string transformation. Preserves the docstring (first statement if
    a bare string literal) and ``def`` signature; replaces everything else
    in the function body with ``body_code`` lines, normalized to 4-space
    indent (one standard indent level under the def).

    ``body_code`` may have any indentation; it's dedented first, then
    re-indented to 4 spaces uniformly. Multi-line code is supported —
    continuation lines get the same 4-space indent + any relative indent
    the dedented source had.

    Raises:
      - :class:`ValueError` if ``test_name`` isn't found
      - :class:`ValueError` if ``body_code`` is empty or parses to nothing
      - :class:`SyntaxError` if the resulting file wouldn't parse
        (caller can surface this to the LLM for retry)

    Used by the ``fill_test_body`` inner tool — the framework owns the
    surgical edit so the 7B can just emit the body as plain Python text
    without wrestling with edit_file_replace's whitespace matching.
    """
    import textwrap

    if not body_code or not body_code.strip():
        raise ValueError("body_code must be non-empty")

    tree = ast.parse(source)
    target: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == test_name:
                target = node
                break
    if target is None:
        raise ValueError(f"no function named {test_name!r} in source")

    # Dedent body_code then re-indent uniformly to 4 spaces.
    body_dedented = textwrap.dedent(body_code).strip("\n")
    body_indented = textwrap.indent(body_dedented, "    ")
    # Validate body_code is parseable Python (so we fail fast with a clean
    # error before touching the file). Wrap in a stub def for parseability.
    try:
        ast.parse("def _probe():\n" + body_indented + "\n")
    except SyntaxError as exc:
        raise SyntaxError(f"body_code is not valid python: {exc}") from exc
    if not body_indented.endswith("\n"):
        body_indented += "\n"

    # Line math (1-based AST line numbers, 0-based list indices).
    lines = source.splitlines(keepends=True)
    body_stmts = target.body
    # Detect & preserve docstring.
    has_docstring = (
        body_stmts
        and isinstance(body_stmts[0], ast.Expr)
        and isinstance(body_stmts[0].value, ast.Constant)
        and isinstance(body_stmts[0].value.value, str)
    )
    if has_docstring:
        # Keep lines up through the docstring's last line; replace everything
        # from the statement after the docstring through the function end.
        keep_through_idx = body_stmts[0].end_lineno  # 1-based last docstring line
        if len(body_stmts) == 1:
            # Docstring is the only statement → there's no body after it.
            # Append after the docstring line.
            cut_end_idx = keep_through_idx
        else:
            cut_end_idx = target.end_lineno  # 1-based last body line
    else:
        keep_through_idx = target.lineno  # the ``def`` line itself
        cut_end_idx = target.end_lineno
    # Slice: lines[0:keep_through_idx] keeps through line keep_through_idx.
    # lines[cut_end_idx:] starts from the line AFTER cut_end_idx.
    new_text = (
        "".join(lines[:keep_through_idx])
        + body_indented
        + "".join(lines[cut_end_idx:])
    )
    # Final parseability check on the reconstructed file.
    try:
        ast.parse(new_text)
    except SyntaxError as exc:
        raise SyntaxError(
            f"replacing body of {test_name!r} produced invalid python: {exc}"
        ) from exc
    return new_text


__all__ = [
    "parse_deliverables",
    "discover_modules",
    "snake_from_deliverable",
    "render_scaffold",
    "replace_test_body",
]
