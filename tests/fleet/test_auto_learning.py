"""Unit tests for the failure-driven learning synthesizer."""

from __future__ import annotations

from datetime import UTC, datetime

from agora.core.types import LearningCategory
from agora.fleet.auto_learning import (
    AUTO_LEARNING_CONFIDENCE,
    AUTO_LEARNING_MARKER,
    synthesize_failure_learning,
)

FIXED_TIME = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)


def test_synthesize_returns_failure_category() -> None:
    learning = synthesize_failure_learning(
        task_id="write_requirements",
        predicate_name="requirements_txt_parses",
        reason="line 1: 'import discord': invalid requirement",
        now=FIXED_TIME,
    )
    assert learning.category == LearningCategory.FAILURE


def test_synthesize_confidence_is_ground_truth_level() -> None:
    learning = synthesize_failure_learning(
        task_id="t", predicate_name="p", reason="r", now=FIXED_TIME
    )
    assert learning.confidence == AUTO_LEARNING_CONFIDENCE
    assert AUTO_LEARNING_CONFIDENCE >= 0.8


def test_synthesize_includes_marker_and_task_and_predicate() -> None:
    learning = synthesize_failure_learning(
        task_id="build_ping",
        predicate_name="bot_py_imports",
        reason="ImportError: no module named 'foo'",
        now=FIXED_TIME,
    )
    assert AUTO_LEARNING_MARKER in learning.content
    assert "build_ping" in learning.content
    assert "bot_py_imports" in learning.content
    assert "ImportError" in learning.content


def test_synthesize_id_is_deterministic_for_same_failure() -> None:
    a = synthesize_failure_learning(
        task_id="t1",
        predicate_name="p1",
        reason="something broke",
        now=FIXED_TIME,
    )
    b = synthesize_failure_learning(
        task_id="t1",
        predicate_name="p1",
        reason="something broke",
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert a.id == b.id, "same failure must produce same id across time"


def test_synthesize_id_differs_across_predicates() -> None:
    a = synthesize_failure_learning(
        task_id="t", predicate_name="p_a", reason="r", now=FIXED_TIME
    )
    b = synthesize_failure_learning(
        task_id="t", predicate_name="p_b", reason="r", now=FIXED_TIME
    )
    assert a.id != b.id


def test_synthesize_normalises_path_noise_for_dedup() -> None:
    """Same predicate failing on different tmp paths must dedup to the same id."""
    a = synthesize_failure_learning(
        task_id="t",
        predicate_name="p",
        reason="file 'C:\\Users\\alice\\repo\\x.py' does not exist",
        now=FIXED_TIME,
    )
    b = synthesize_failure_learning(
        task_id="t",
        predicate_name="p",
        reason="file 'C:\\Users\\bob\\other\\x.py' does not exist",
        now=FIXED_TIME,
    )
    assert a.id == b.id


def test_synthesize_shortens_very_long_reason() -> None:
    long_reason = "x" * 5000
    learning = synthesize_failure_learning(
        task_id="t", predicate_name="p", reason=long_reason, now=FIXED_TIME
    )
    assert len(learning.content) < 1000
    assert learning.content.endswith("...")


def test_synthesize_id_is_auto_prefixed() -> None:
    learning = synthesize_failure_learning(
        task_id="t", predicate_name="p", reason="r", now=FIXED_TIME
    )
    assert learning.id.startswith("auto-")


def test_synthesize_preserves_task_ref() -> None:
    learning = synthesize_failure_learning(
        task_id="integration_check",
        predicate_name="pytest_test_bot_py",
        reason="pytest failed for test_bot.py",
        now=FIXED_TIME,
    )
    assert learning.task_ref == "integration_check"
