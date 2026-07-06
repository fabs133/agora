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
import json
from pathlib import Path

#: Fence tag for a serialized, re-runnable run_check in the Verification record
#: (F20). A phase-0 re-validator (or the round-trip test) parses these back into
#: run_check predicate args and executes them — cmd + stdin + expectation intact.
RUN_CHECK_FENCE = "run_check"

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

#: The four headers the model must supply, one per micro-task (run 2.5).
PROSE_HEADERS: tuple[str, ...] = tuple(h for h, kind in SECTION_PLAN if kind == "prose")
#: The four headers assembled mechanically.
FACT_HEADERS: tuple[str, ...] = tuple(h for h, kind in SECTION_PLAN if kind == "fact")

#: Prose header -> the micro-task body file under ``prose/`` (run 2.5: T9.2a-d).
PROSE_FILE_MAP: dict[str, str] = {
    "## Architecture & invariants": "architecture.md",
    "## Conventions": "conventions.md",
    "## Extension points": "extension_points.md",
    "## How to run / test": "how_to_run.md",
}
#: Body used when a prose section's micro-task human-fallbacked (its file is
#: absent). Marked so the fact-check can tell model prose from human-pending.
HUMAN_FALLBACK_BODY = "_(human)_ — section pending human authorship (micro-task derailed)."

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

def _human_command_line(chk: dict) -> str:
    """A ``# ``-prefixed human-readable one-liner for a run_check: the joined
    command + its stdin/expectation. Keeps the raw command STRING (``pytest -q``,
    ``python -m echobot``) present in the doc for readers and substring gates,
    alongside the machine-parseable JSON below it. Embedded newlines in the command
    (a multi-line ``python -c`` snippet, e.g. the FakeGateway round-trip) are
    collapsed to ``\\n`` so this stays exactly ONE physical line — otherwise the
    continuation lines would not be ``#``-prefixed and would leak into the JSON body
    the parser reconstructs."""
    cmd = " ".join(chk.get("cmd", [])).replace("\n", "\\n")
    bits: list[str] = []
    if chk.get("stdin"):
        bits.append(f"stdin={json.dumps(chk['stdin'])}")
    if chk.get("expect_stdout_contains"):
        bits.append(f'stdout contains "{chk["expect_stdout_contains"]}"')
    if "expect_exit" in chk and not chk.get("expect_stdout_contains"):
        bits.append(f"exit {chk['expect_exit']}")
    return f"# {cmd}" + (f"   ({'; '.join(bits)})" if bits else "")


def _verification_record(gate_checks: list[dict]) -> str:
    """Serialize the verification record (F20): one FENCED, re-runnable run_check
    per gate — a human command comment plus the full ``cmd`` + ``stdin`` +
    expectation as JSON (not bare argv) — so a phase-0 re-validator can round-trip
    each entry back through the run_check predicate."""
    if not gate_checks:
        return "_No gate checks recorded._"
    lines = ["Gate checks (re-run each verbatim in any future phase-0 re-validation):", ""]
    for chk in gate_checks:
        lines.append(f"```{RUN_CHECK_FENCE}")
        lines.append(_human_command_line(chk))
        lines.append(json.dumps(chk, ensure_ascii=False, sort_keys=True))
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip()


def parse_verification_run_checks(text: str) -> list[dict]:
    """Inverse of :func:`_verification_record`: extract the run_check arg dicts
    from a Verification-record body's fenced blocks. A phase-0 re-validator feeds
    each straight into ``build_predicate("run_check", spec)``."""
    out: list[dict] = []
    lines = (text or "").splitlines()
    i = 0
    fence_open = f"```{RUN_CHECK_FENCE}"
    while i < len(lines):
        if lines[i].strip() == fence_open:
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != "```":
                if not lines[i].strip().startswith("#"):  # drop the human comment line
                    body.append(lines[i])
                i += 1
            try:
                spec = json.loads("\n".join(body))
                if isinstance(spec, dict):
                    out.append(spec)
            except json.JSONDecodeError:
                pass
        i += 1
    return out


def extract_fact_sections(workspace: Path, gate_checks: list[dict]) -> dict[str, str]:
    """Generate the four FACT section bodies from the workspace tree + the flow's
    gate CHECKS (full run_check specs, not bare argv — F20). Keys are the FACT
    headers in :data:`FACT_HEADERS`."""
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

    verification = _verification_record(gate_checks)

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


def read_prose_sections(prose_dir: Path) -> dict[str, str]:
    """Read the four prose micro-task body files from ``prose_dir`` into
    ``{"## Header": body}``. A missing or empty file (its micro-task
    human-fallbacked) yields the ``(human)`` placeholder so the assembled document
    stays structurally complete and the gap is visibly marked."""
    prose_dir = Path(prose_dir)
    out: dict[str, str] = {}
    for header, fname in PROSE_FILE_MAP.items():
        body = _read(prose_dir / fname).strip() if (prose_dir / fname).exists() else ""
        out[header] = body or HUMAN_FALLBACK_BODY
    return out


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
    gate_checks: list[dict],
    prose_dir: Path,
    out_path: Path,
) -> str:
    """Assemble ``PROJECT_STATE.md`` = mechanical FACT + the four PROSE micro-task
    files (``prose_dir``) and write it to ``out_path`` as UTF-8 (F20b). Returns the
    assembled text. Pure/deterministic given the workspace tree, gate checks, and
    prose files; a missing prose file becomes a marked ``(human)`` placeholder."""
    fact = extract_fact_sections(Path(workspace), gate_checks)
    prose = read_prose_sections(Path(prose_dir))
    assembled = assemble(fact, prose)
    Path(out_path).write_text(assembled, encoding="utf-8")  # F20b: pinned utf-8
    return assembled


__all__ = [
    "SECTION_PLAN", "PROSE_HEADERS", "FACT_HEADERS", "PROSE_FILE_MAP",
    "HUMAN_FALLBACK_BODY", "RUN_CHECK_FENCE", "python_signatures",
    "extract_fact_sections", "parse_prose_sections", "read_prose_sections",
    "parse_verification_run_checks", "assemble", "write_project_state",
]
