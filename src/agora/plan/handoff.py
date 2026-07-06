"""Agora handoff — mechanical PROJECT_STATE.md assembly (integration run 2.4).

Run 2's evidence (findings F18'' + Part 11) split the handoff document into two
kinds of section:

* **FACT** sections — Identity, Capability inventory, Verification record, File
  map — are mechanically DERIVABLE from the workspace + the flow's gate commands.
  Emitting them from a model is both a reliability risk (the reasoning-vs-action
  emission gap at document scale) and a correctness risk (hallucinated file maps
  / signatures). They are generated here, true-by-construction.
* **PROSE** sections — Architecture & invariants, Conventions, Extension points,
  How to run / test — carry judgement a model writes at README scale (T9.2).

:func:`write_project_state` reads the model's prose file, extracts the FACT
sections, and assembles ``PROJECT_STATE.md`` deterministically in the eight
mandatory headers' canonical order. Everything here is pure over its inputs
(workspace tree, gate commands, prose text) so it is unit-testable against a real
workspace fixture and re-runnable in any future phase-0 re-validation.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: The eight mandatory headers, in order, tagged by source. FACT headers are
#: generated here; PROSE headers are lifted from the model's prose file.
SECTION_PLAN: tuple[tuple[str, str], ...] = (
    ("## Identity", "fact"),
    ("## Architecture & invariants", "prose"),
    ("## Capability inventory", "fact"),
    ("## Verification record", "fact"),
    ("## File map", "fact"),
    ("## Conventions", "prose"),
    ("## Extension points", "prose"),
    ("## How to run / test", "prose"),
)

#: The four headers the model (T9.2) must supply in PROJECT_STATE.prose.md.
PROSE_HEADERS: tuple[str, ...] = tuple(h for h, kind in SECTION_PLAN if kind == "prose")
#: The four headers assembled mechanically.
FACT_HEADERS: tuple[str, ...] = tuple(h for h, kind in SECTION_PLAN if kind == "fact")

_SKIP_DIRS = {".git", "__pycache__", "verdicts", ".knowledge", ".pytest_cache", ".mypy_cache"}
_SKIP_FILES = {"PROJECT_STATE.md", "PROJECT_STATE.prose.md"}


# ------------------------------------------------------------------ signatures

def _func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    args = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix}{node.name}({args}){ret}"


def python_signatures(source: str) -> list[str]:
    """Top-level function/class signatures (with annotations) from Python source.

    Class methods are indented under their class. A syntax error yields ``[]`` —
    the extractor degrades to "file present, no signatures" rather than raising."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    sigs: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            sigs.append(_func_signature(node))
        elif isinstance(node, ast.ClassDef):
            sigs.append(f"class {node.name}")
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef):
                    sigs.append(f"    {_func_signature(sub)}")
    return sigs


def _iter_source_files(workspace: Path) -> list[tuple[str, Path]]:
    """(_posix_relpath, abspath) for every non-noise file under ``workspace``,
    sorted for deterministic output."""
    out: list[tuple[str, Path]] = []
    for p in sorted(workspace.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(workspace)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if p.name in _SKIP_FILES:
            continue
        out.append((rel.as_posix(), p))
    return out


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# ------------------------------------------------------------------ FACT sections

def extract_fact_sections(workspace: Path, gate_commands: list[str]) -> dict[str, str]:
    """Generate the four FACT section bodies from the workspace tree + the flow's
    gate commands. Keys are the FACT headers in :data:`FACT_HEADERS`."""
    workspace = Path(workspace)
    files = _iter_source_files(workspace)
    packages = sorted({rel.split("/")[0] for rel, p in files if p.name == "__init__.py"})
    name = packages[0] if packages else workspace.name
    runnable = bool(packages) and (workspace / name / "__main__.py").exists()

    identity = f"**{name}** — Python package."
    if runnable:
        identity += f" Runnable module (`python -m {name}`)."

    # Capability inventory: public top-level signatures per non-test module.
    cap_lines: list[str] = []
    for rel, p in files:
        if p.suffix != ".py" or Path(rel).name.startswith("test_"):
            continue
        sigs = [s for s in python_signatures(_read(p)) if s.strip()]
        public = [s for s in sigs if not s.strip().split()[-1].lstrip("cdef ").startswith("_")
                  or "class " in s]
        if public:
            cap_lines.append(f"`{rel}`:")
            cap_lines.extend(f"- `{s.strip()}`" for s in public)
    capability = "\n".join(cap_lines) if cap_lines else "_None discovered._"

    verification = (
        "Gate commands (re-run verbatim in any future phase-0 re-validation):\n\n"
        + "\n".join(f"- `{c}`" for c in gate_commands)
        if gate_commands else "_No gate commands recorded._"
    )

    map_lines: list[str] = []
    for rel, p in files:
        if p.suffix == ".py":
            top = [s.split("(")[0].replace("def ", "").replace("class ", "").replace("async ", "").strip()
                   for s in python_signatures(_read(p)) if not s.startswith("    ")]
            map_lines.append(f"- `{rel}`" + (f" — {', '.join(top)}" if top else ""))
        else:
            map_lines.append(f"- `{rel}`")
    file_map = "\n".join(map_lines) if map_lines else "_Empty workspace._"

    return {
        "## Identity": identity,
        "## Capability inventory": capability,
        "## Verification record": verification,
        "## File map": file_map,
    }


# ------------------------------------------------------------------ prose + assemble

def parse_prose_sections(text: str) -> dict[str, str]:
    """Split a prose markdown document into ``{"## Header": body}`` by ``## ``
    headers (case- and whitespace-sensitive so headers match the mandatory set)."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line.strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def assemble(fact_sections: dict[str, str], prose_sections: dict[str, str]) -> str:
    """Interleave FACT and PROSE sections into the eight-header canonical order.
    Every mandatory header is emitted even if its body is empty (a missing prose
    section yields a placeholder, so the structural gate reds loudly rather than
    silently dropping a header)."""
    parts: list[str] = []
    for header, kind in SECTION_PLAN:
        source = fact_sections if kind == "fact" else prose_sections
        body = (source.get(header) or "").strip() or "_(section not provided)_"
        parts.append(f"{header}\n\n{body}")
    return "\n\n".join(parts) + "\n"


def write_project_state(
    workspace: Path,
    gate_commands: list[str],
    prose_path: Path,
    out_path: Path,
) -> str:
    """Assemble ``PROJECT_STATE.md`` = mechanical FACT + the model's PROSE and
    write it to ``out_path``. Returns the assembled text. Pure/deterministic given
    the workspace tree, gate commands, and prose file."""
    prose_text = _read(Path(prose_path)) if Path(prose_path).exists() else ""
    fact = extract_fact_sections(Path(workspace), gate_commands)
    prose = parse_prose_sections(prose_text)
    assembled = assemble(fact, prose)
    Path(out_path).write_text(assembled, encoding="utf-8")
    return assembled


__all__ = [
    "SECTION_PLAN", "PROSE_HEADERS", "FACT_HEADERS",
    "python_signatures", "extract_fact_sections", "parse_prose_sections",
    "assemble", "write_project_state",
]
