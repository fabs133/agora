"""Complexity-gate postconditions: ``max_line_length`` and ``no_task_exceeds_complexity``.

The mechanism is the capability being tested — we want to confirm the postcond
fires on oversized input and passes on compact input. Whether the 7B planner
can *act* on the learning-injection failure is a different question (see
Stage 4 risks in the plan doc).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agora.plan.predicate_registry import (
    build_predicate,
    postcond_max_line_length,
    postcond_no_task_exceeds_complexity,
)


def test_max_line_length_passes_on_short_lines(tmp_path: Path):
    (tmp_path / "tasks.md").write_text(
        "# Tasks\n\n- t1: do thing one\n- t2: do thing two\n", encoding="utf-8"
    )
    p = postcond_max_line_length("tasks.md", max_chars=80)
    passed, _ = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is True


def test_max_line_length_fails_on_long_line(tmp_path: Path):
    long = "- fat_task: " + "x" * 200
    (tmp_path / "tasks.md").write_text(f"# Tasks\n\n{long}\n", encoding="utf-8")
    p = postcond_max_line_length("tasks.md", max_chars=120)
    passed, reason = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "split them" in reason


def test_max_line_length_missing_file(tmp_path: Path):
    p = postcond_max_line_length("nope.md", max_chars=80)
    passed, reason = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "does not exist" in reason


def test_max_line_length_via_registry(tmp_path: Path):
    (tmp_path / "ok.md").write_text("short line\n", encoding="utf-8")
    p = build_predicate("max_line_length", {"rel": "ok.md", "max_chars": 50})
    passed, _ = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is True


def test_no_task_exceeds_complexity_passes_on_small_plan(tmp_path: Path):
    plan = {
        "version": "2.0",
        "name": "mini",
        "task_graph": [
            {
                "id": "t1",
                "description": "do one thing",
                "stages": [{"name": "s", "instruction": "step"}],
                "postconditions": [{"name": "mark_complete", "args": {}}],
            }
        ],
    }
    (tmp_path / "plan.yaml").write_text(yaml.safe_dump(plan), encoding="utf-8")
    p = postcond_no_task_exceeds_complexity(
        "plan.yaml", max_chars=500, max_stages=3, max_postconditions=8
    )
    passed, _ = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is True


def test_no_task_exceeds_complexity_fails_on_oversized_instruction(tmp_path: Path):
    bloat = "x" * 5000
    plan = {
        "version": "2.0",
        "name": "fat",
        "task_graph": [
            {
                "id": "big_task",
                "description": "fat task",
                "stages": [{"name": "s1", "instruction": bloat}],
                "postconditions": [],
            }
        ],
    }
    (tmp_path / "plan.yaml").write_text(yaml.safe_dump(plan), encoding="utf-8")
    p = postcond_no_task_exceeds_complexity(
        "plan.yaml", max_chars=1000, max_stages=3, max_postconditions=8
    )
    passed, reason = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "big_task" in reason
    assert "split" in reason.lower()


def test_no_task_exceeds_complexity_fails_on_too_many_stages(tmp_path: Path):
    plan = {
        "version": "2.0",
        "name": "many_stages",
        "task_graph": [
            {
                "id": "t",
                "description": "too many stages",
                "stages": [
                    {"name": f"s{i}", "instruction": "x"} for i in range(6)
                ],
                "postconditions": [],
            }
        ],
    }
    (tmp_path / "plan.yaml").write_text(yaml.safe_dump(plan), encoding="utf-8")
    p = postcond_no_task_exceeds_complexity(
        "plan.yaml", max_chars=10000, max_stages=3, max_postconditions=8
    )
    passed, reason = p.evaluate({"work_dir": str(tmp_path)})
    assert passed is False
    assert "stages" in reason
