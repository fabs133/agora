"""AST-aware upsert helpers for Python source files.

Sprint 7.4 — the implementer equivalent of Sprint 7.3's ``fill_test_body``.
Observed failure: 7B can reliably author an INITIAL Python module but can't
combine multiple ``edit_file_replace`` / ``edit_file_append`` calls into a
coherent file when adding more code. The file accumulates half-finished
class definitions stacked on top of each other (see live run logs:
``add_core_domain_module`` produced 5 duplicate class definitions over 49
iterations before giving up).

These helpers decompose that work: the model emits the new top-level
function, class, or class-method source, and the framework owns the
positional surgery — append if new, replace if a definition with the
same name already exists. The model never has to match whitespace
against the existing file or keep track of where anything goes.

All three functions are pure string transformations; they parse the
source with :mod:`ast` and manipulate the line list in place. Callers
catch :class:`ValueError` for "target not found" and :class:`SyntaxError`
for "your code doesn't parse" — both are recoverable by the model on
the next turn.
"""

from __future__ import annotations

import ast
import textwrap


def _normalize_code(code: str) -> str:
    """Strip leading/trailing blank lines, then dedent.

    Guards against the common 7B failure mode: the model passes pre-indented
    code with a leading newline (e.g. ``"\\n    def foo(): ..."``). The
    empty first line makes :func:`textwrap.dedent` compute a common
    leading whitespace of 0 — it leaves the indented lines alone and
    parsing fails with "unexpected indent". Stripping blank lines first
    makes dedent see a clean block.
    """
    if not code:
        return code
    lines = code.split("\n")
    # Drop leading blank lines.
    while lines and not lines[0].strip():
        lines.pop(0)
    # Drop trailing blank lines.
    while lines and not lines[-1].strip():
        lines.pop()
    stripped = "\n".join(lines)
    return textwrap.dedent(stripped)


def _find_toplevel(tree: ast.Module, name: str, kinds: tuple[type, ...]) -> ast.AST | None:
    """Walk ``tree.body`` looking for a top-level def of ``name`` in ``kinds``."""
    for node in tree.body:
        if isinstance(node, kinds) and getattr(node, "name", None) == name:
            return node
    return None


def _node_line_span(node: ast.AST) -> tuple[int, int]:
    """Return the 1-based inclusive (first_line, last_line) span of ``node``.

    Respects decorators — ``ast.FunctionDef.lineno`` is the ``def`` line,
    NOT the first decorator line. We widen to the earliest decorator if any.
    """
    first = node.lineno
    decorators = getattr(node, "decorator_list", None) or []
    for dec in decorators:
        if dec.lineno < first:
            first = dec.lineno
    last = node.end_lineno
    return first, last


def _replace_lines(
    source: str, first_line: int, last_line: int, replacement: str
) -> str:
    """Replace ``source`` lines ``[first_line..last_line]`` (1-based, inclusive)
    with ``replacement`` (which should end in a newline). Returns new source."""
    lines = source.splitlines(keepends=True)
    if not replacement.endswith("\n"):
        replacement += "\n"
    return (
        "".join(lines[: first_line - 1])
        + replacement
        + "".join(lines[last_line:])
    )


def _append_block(source: str, block: str) -> str:
    """Append ``block`` to ``source`` with a blank-line separator."""
    if not source:
        return block if block.endswith("\n") else block + "\n"
    if not source.endswith("\n"):
        source += "\n"
    # Two newlines for PEP 8 between top-level defs; caller's problem to
    # use less separation if appending a method (already indented).
    if not source.endswith("\n\n"):
        source += "\n"
    if not block.endswith("\n"):
        block += "\n"
    return source + block


def _validate_single_def(
    code: str, expected: tuple[type, ...], label: str
) -> ast.AST:
    """Parse ``code`` and assert it contains exactly ONE top-level node of
    a type in ``expected``. Returns that node. Raises ValueError / SyntaxError
    with clear messages."""
    try:
        tree = ast.parse(_normalize_code(code))
    except SyntaxError as exc:
        raise SyntaxError(f"{label} code is not valid python: {exc}") from exc
    # Strip empty string-expr "docstrings" at module level.
    real_body = [
        n for n in tree.body
        if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
    ]
    if len(real_body) != 1:
        raise ValueError(
            f"{label} code must contain exactly one {label} definition; "
            f"got {len(real_body)} top-level statement(s)"
        )
    node = real_body[0]
    if not isinstance(node, expected):
        raise ValueError(
            f"{label} code must define a {label}; got {type(node).__name__}"
        )
    return node


def upsert_function(source: str, code: str) -> str:
    """Add a top-level function to ``source`` or replace an existing one.

    ``code`` must be the complete Python source for ONE function (including
    the ``def`` line, body, and optionally decorators). If a top-level
    function with the same name already exists, it's replaced in place;
    otherwise the new function is appended at the end of the module.

    Returns the new module source. Raises :class:`ValueError` if ``code``
    doesn't define exactly one function. Raises :class:`SyntaxError` if
    ``code`` or the resulting module won't parse.
    """
    new_node = _validate_single_def(
        code, (ast.FunctionDef, ast.AsyncFunctionDef), "function"
    )
    name = new_node.name
    # Normalise code — strip leading/trailing blank lines, but keep content.
    block = _normalize_code(code).strip("\n") + "\n"
    try:
        tree = ast.parse(source) if source.strip() else ast.parse("")
    except SyntaxError as exc:
        raise SyntaxError(
            f"existing module source is not valid python: {exc}"
        ) from exc
    existing = _find_toplevel(
        tree, name, (ast.FunctionDef, ast.AsyncFunctionDef)
    )
    if existing is not None:
        first, last = _node_line_span(existing)
        new_source = _replace_lines(source, first, last, block)
    else:
        new_source = _append_block(source, block)
    _validate_parses(new_source, f"upsert_function({name!r})")
    return new_source


def upsert_class(source: str, code: str) -> str:
    """Add a top-level class to ``source`` or replace an existing one.

    Same semantics as :func:`upsert_function` but for classes.
    """
    new_node = _validate_single_def(code, (ast.ClassDef,), "class")
    name = new_node.name
    block = _normalize_code(code).strip("\n") + "\n"
    try:
        tree = ast.parse(source) if source.strip() else ast.parse("")
    except SyntaxError as exc:
        raise SyntaxError(
            f"existing module source is not valid python: {exc}"
        ) from exc
    existing = _find_toplevel(tree, name, (ast.ClassDef,))
    if existing is not None:
        first, last = _node_line_span(existing)
        new_source = _replace_lines(source, first, last, block)
    else:
        new_source = _append_block(source, block)
    _validate_parses(new_source, f"upsert_class({name!r})")
    return new_source


def upsert_class_method(source: str, class_name: str, code: str) -> str:
    """Add a method inside ``class_name`` or replace an existing method.

    ``code`` must be the source for ONE ``def`` or ``async def`` (the method,
    without the enclosing class — framework re-indents). If a method with
    the same name exists on the class, it's replaced at the same position;
    otherwise the method is appended to the end of the class body.

    Raises :class:`ValueError` if the class doesn't exist in ``source``,
    or if ``code`` doesn't define exactly one function.
    """
    new_node = _validate_single_def(
        code, (ast.FunctionDef, ast.AsyncFunctionDef), "method"
    )
    method_name = new_node.name
    if not source.strip():
        raise ValueError(
            f"cannot add method {method_name!r} — source is empty and "
            f"has no {class_name!r} class. Use upsert_class first."
        )
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SyntaxError(
            f"existing module source is not valid python: {exc}"
        ) from exc
    target_class = _find_toplevel(tree, class_name, (ast.ClassDef,))
    if target_class is None:
        raise ValueError(
            f"class {class_name!r} not found in source. Use upsert_class "
            f"to create it first."
        )

    # Re-indent the method source to 4 spaces (standard class-body indent).
    method_source = _normalize_code(code).strip("\n")
    method_indented = textwrap.indent(method_source, "    ") + "\n"

    # Find existing method with the same name inside the class.
    existing_method: ast.AST | None = None
    for sub in target_class.body:
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if sub.name == method_name:
                existing_method = sub
                break

    if existing_method is not None:
        first, last = _node_line_span(existing_method)
        new_source = _replace_lines(source, first, last, method_indented)
    else:
        # Append after the last line of the class body.
        class_last_line = target_class.end_lineno
        lines = source.splitlines(keepends=True)
        # Ensure one blank line between the previous class member and the new one.
        prefix = "".join(lines[:class_last_line])
        if not prefix.endswith("\n"):
            prefix += "\n"
        suffix = "".join(lines[class_last_line:])
        new_source = prefix + "\n" + method_indented + suffix
    _validate_parses(
        new_source, f"upsert_class_method({class_name!r}, {method_name!r})"
    )
    return new_source


def _validate_parses(source: str, label: str) -> None:
    """Raise SyntaxError with context if ``source`` doesn't parse."""
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise SyntaxError(
            f"{label} produced invalid python: {exc}"
        ) from exc


__all__ = [
    "upsert_function",
    "upsert_class",
    "upsert_class_method",
]
