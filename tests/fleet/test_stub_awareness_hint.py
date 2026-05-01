"""Sprint 7.5(b): stub-awareness hint in user message for implementer tasks."""

from __future__ import annotations

from pathlib import Path

from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import TaskStatus
from agora.fleet.agent_runtime import _build_stub_awareness_hint


def _task(output_path: str) -> Task:
    return Task(
        id="t",
        spec=Specification(
            postconditions=(make_predicate("_t", "pass", lambda _c: (True, "")),),
            description="implement",
        ),
        description="implement",
        agent_id="impl",
        status=TaskStatus.PENDING,
        output_path=output_path,
    )


def test_hint_fires_on_stub_file(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "cli.py").write_text(
        '"""Implementation stubs — bodies filled in by implementer."""\n\n'
        "def shorten(url: str) -> str:\n"
        "    raise NotImplementedError\n",
        encoding="utf-8",
    )
    hint = _build_stub_awareness_hint(_task("src/cli.py"), str(tmp_path))
    assert "STUB" in hint
    assert "src/cli.py" in hint
    assert "add_function" in hint
    assert "add_class_method" in hint
    assert "NotImplementedError" in hint


def test_hint_empty_when_file_absent(tmp_path: Path):
    hint = _build_stub_awareness_hint(_task("src/missing.py"), str(tmp_path))
    assert hint == ""


def test_hint_empty_when_file_not_a_stub(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.py").write_text(
        "def shorten(url: str) -> str:\n"
        "    return url[:6]\n",
        encoding="utf-8",
    )
    hint = _build_stub_awareness_hint(_task("src/real.py"), str(tmp_path))
    assert hint == ""


def test_hint_empty_when_output_path_not_src(tmp_path: Path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text(
        "def test_x():\n    raise NotImplementedError\n",
        encoding="utf-8",
    )
    # Even though the file has NotImplementedError, test tasks get their own
    # scaffolding path — don't inject the impl-specific hint.
    hint = _build_stub_awareness_hint(_task("tests/test_x.py"), str(tmp_path))
    assert hint == ""


def test_hint_empty_when_output_path_empty(tmp_path: Path):
    hint = _build_stub_awareness_hint(_task(""), str(tmp_path))
    assert hint == ""


def test_hint_empty_when_output_not_python(tmp_path: Path):
    # Non-.py file even if under src/ shouldn't get the hint.
    hint = _build_stub_awareness_hint(_task("src/config.toml"), str(tmp_path))
    assert hint == ""
