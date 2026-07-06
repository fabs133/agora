"""Handoff extractor (integration run 2.4) — FACT generation + assembly."""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.plan import handoff

GATE_COMMANDS = ["python -m pytest -q", "python -m echobot"]

# The eight mandatory headers + the two gate commands the P9 gate checks on the
# ASSEMBLED PROJECT_STATE.md.
MANDATORY_HEADERS = [
    "## Identity", "## Architecture & invariants", "## Capability inventory",
    "## Verification record", "## File map", "## Conventions",
    "## Extension points", "## How to run / test",
]


def _mk_workspace(root: Path) -> Path:
    """A faithful mirror of the run-2 echobot workspace (real code shapes:
    module-level handle_message with annotations, an importing __main__)."""
    (root / "echobot").mkdir(parents=True)
    (root / "echobot" / "__init__.py").write_text("", encoding="utf-8")
    (root / "echobot" / "core.py").write_text(
        "import random\n"
        "from typing import Optional\n\n"
        "def handle_message(text: str, rng: random.Random) -> Optional[str]:\n"
        "    return None\n",
        encoding="utf-8",
    )
    (root / "echobot" / "__main__.py").write_text(
        "import sys\n"
        "from echobot.core import handle_message\n\n"
        "def main() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_core.py").write_text(
        "def test_ping():\n    assert True\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# echobot\n", encoding="utf-8")
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (root / "verdicts").mkdir()
    (root / "verdicts" / "p9.json").write_text("{}", encoding="utf-8")  # noise → skipped
    return root


def test_python_signatures_carries_annotations() -> None:
    src = "def handle_message(text: str, rng: random.Random) -> str | None:\n    ...\n"
    sigs = handoff.python_signatures(src)
    assert sigs == ["def handle_message(text: str, rng: random.Random) -> str | None"]


def test_extract_fact_sections_over_real_workspace_shape(tmp_path) -> None:
    ws = _mk_workspace(tmp_path)
    fact = handoff.extract_fact_sections(ws, GATE_COMMANDS)
    assert set(fact) == set(handoff.FACT_HEADERS)
    # Identity: package name + runnable
    assert "echobot" in fact["## Identity"] and "python -m echobot" in fact["## Identity"]
    # Capability inventory: the real handle_message signature (from AST)
    assert "handle_message(text: str, rng: random.Random)" in fact["## Capability inventory"]
    # Verification record: the gate commands verbatim
    assert "python -m pytest -q" in fact["## Verification record"]
    assert "python -m echobot" in fact["## Verification record"]
    # File map: real files present, verdicts/ noise excluded
    assert "`echobot/core.py`" in fact["## File map"]
    assert "`echobot/__main__.py`" in fact["## File map"]
    assert "verdicts/" not in fact["## File map"]


def test_parse_prose_sections_splits_by_header() -> None:
    prose = (
        "## Architecture & invariants\nPure core + thin adapter.\n\n"
        "## Conventions\nSnake case.\n\n"
        "## Extension points\nAdd a command.\n\n"
        "## How to run / test\nRun it.\n"
    )
    got = handoff.parse_prose_sections(prose)
    assert set(got) == set(handoff.PROSE_HEADERS)
    assert got["## Architecture & invariants"] == "Pure core + thin adapter."


def test_assemble_produces_all_eight_headers_in_order(tmp_path) -> None:
    ws = _mk_workspace(tmp_path)
    fact = handoff.extract_fact_sections(ws, GATE_COMMANDS)
    prose = {h: f"prose for {h}" for h in handoff.PROSE_HEADERS}
    doc = handoff.assemble(fact, prose)
    # every mandatory header present
    for h in MANDATORY_HEADERS:
        assert h in doc
    # canonical order preserved
    positions = [doc.index(h) for h in MANDATORY_HEADERS]
    assert positions == sorted(positions)


def _write_prose_files(prose_dir: Path, present: set[str]) -> None:
    """Write body files for the prose headers in ``present`` (by short name)."""
    prose_dir.mkdir(parents=True, exist_ok=True)
    for header, fname in handoff.PROSE_FILE_MAP.items():
        short = fname.removesuffix(".md")
        if short in present:
            (prose_dir / fname).write_text(
                f"Concrete body for {header} — at least eighty characters of real "
                f"project prose here for the length gate.", encoding="utf-8")


def test_four_file_merge_uses_all_prose(tmp_path) -> None:
    """Item 2: a 4-file merge puts each micro-task's body under its header; no
    fallback placeholder appears."""
    ws = _mk_workspace(tmp_path)
    prose_dir = ws / "prose"
    _write_prose_files(prose_dir, {"architecture", "conventions", "extension_points", "how_to_run"})
    out = ws / "PROJECT_STATE.md"
    handoff.write_project_state(ws, GATE_COMMANDS, prose_dir, out)
    doc = out.read_text(encoding="utf-8")
    for predicate in [*MANDATORY_HEADERS, "python -m pytest -q", "python -m echobot"]:
        assert predicate in doc, f"P9 gate predicate missing: {predicate}"
    assert "Concrete body for ## Architecture & invariants" in doc
    assert "(human)" not in doc  # all four present → no fallback


def test_three_file_plus_fallback_merge(tmp_path) -> None:
    """Item 2: a 3-file + 1-missing merge still yields all 8 headers; the missing
    section is a marked (human) fallback, and the gate predicates still hold."""
    ws = _mk_workspace(tmp_path)
    prose_dir = ws / "prose"
    _write_prose_files(prose_dir, {"architecture", "conventions", "how_to_run"})  # extension_points missing
    out = ws / "PROJECT_STATE.md"
    handoff.write_project_state(ws, GATE_COMMANDS, prose_dir, out)
    doc = out.read_text(encoding="utf-8")
    for predicate in [*MANDATORY_HEADERS, "python -m pytest -q", "python -m echobot"]:
        assert predicate in doc, f"P9 gate predicate missing: {predicate}"
    assert handoff.HUMAN_FALLBACK_BODY in doc          # the missing section is flagged
    assert "(human)" in doc
    # exactly one section fell back
    assert doc.count(handoff.HUMAN_FALLBACK_BODY) == 1


def test_missing_prose_section_is_visible_not_silent(tmp_path) -> None:
    """A prose file missing a section still yields all 8 headers, but the gap is a
    visible placeholder (the structural gate stays honest)."""
    ws = _mk_workspace(tmp_path)
    fact = handoff.extract_fact_sections(ws, GATE_COMMANDS)
    doc = handoff.assemble(fact, {"## Conventions": "only this one"})
    assert "## Architecture & invariants" in doc
    assert "_(section not provided)_" in doc


def test_extractor_against_live_run2_workspace_if_present() -> None:
    """Honours the addendum's 'run-2 workspace as fixture' literally when the
    untracked provenance dir exists; skips in a clean checkout / CI."""
    ws = Path("runs_out/integration-run-2/echobot/echobot")
    if not (ws / "echobot" / "core.py").exists():
        pytest.skip("run-2 workspace not present (untracked provenance)")
    fact = handoff.extract_fact_sections(ws, GATE_COMMANDS)
    assert "handle_message" in fact["## Capability inventory"]
    assert "`echobot/core.py`" in fact["## File map"]
