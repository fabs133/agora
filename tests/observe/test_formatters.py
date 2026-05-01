from agora.core.learning import Learning
from agora.core.project import PhaseChange
from agora.core.types import LearningCategory, ProjectPhase
from agora.observe.formatters import (
    FormattedMessage,
    format_ack,
    format_error,
    format_help,
    format_learning,
    format_note,
    format_phase_change,
    format_review_summary,
    format_task_completed,
    format_task_started,
)


def test_phase_change_has_both_plain_and_html() -> None:
    msg = format_phase_change(
        PhaseChange(
            from_phase=ProjectPhase.INIT,
            to_phase=ProjectPhase.ANALYSIS,
            reason="kicking off",
            timestamp="2026-04-15T00:00:00+00:00",
        )
    )
    assert isinstance(msg, FormattedMessage)
    assert "init" in msg.body and "analysis" in msg.body
    assert "<h4>" in msg.formatted_body
    content = msg.to_content()
    assert content["format"] == "org.matrix.custom.html"
    assert content["msgtype"] == "m.notice"


def test_task_started_renders_agent_and_description() -> None:
    msg = format_task_started(
        {"task_id": "abc12345-xyz", "description": "write hello.txt", "agent_id": "impl"}
    )
    assert "abc12345" in msg.body
    assert "impl" in msg.body
    assert "write hello.txt" in msg.body
    assert "<code>impl</code>" in msg.formatted_body


def test_task_completed_success_badge() -> None:
    msg = format_task_completed(
        {
            "task_id": "deadbeefxx",
            "success": True,
            "artifacts": ["a.py"],
            "postcondition_results": [{"name": "ok", "passed": True, "reason": ""}],
        }
    )
    assert "✓" in msg.body
    assert "a.py" in msg.formatted_body


def test_task_completed_failure_lists_reasons() -> None:
    msg = format_task_completed(
        {
            "task_id": "deadbeefxx",
            "success": False,
            "artifacts": [],
            "postcondition_results": [{"name": "needs_file", "passed": False, "reason": "no file"}],
        }
    )
    assert "✗" in msg.body
    # Reason shown in HTML when predicate failed.
    assert "no file" in msg.formatted_body


def test_task_completed_accepts_tuple_postcondition_rows() -> None:
    """The in-memory TaskResult path gives tuples (name, passed, reason)."""
    msg = format_task_completed(
        {
            "task_id": "x",
            "success": True,
            "artifacts": [],
            "postcondition_results": [("ok", True, "")],
        }
    )
    assert "✓" in msg.body


def test_learning_bar_scales_with_confidence() -> None:
    high = format_learning(
        Learning(
            id="a",
            category=LearningCategory.PATTERN,
            content="use DI",
            confidence=0.95,
            task_ref="t",
        )
    )
    low = format_learning(
        Learning(
            id="b",
            category=LearningCategory.FAILURE,
            content="avoid X",
            confidence=0.1,
            task_ref="t",
        )
    )
    # The ASCII bar lives in the HTML rendering; body carries the percent.
    assert high.formatted_body.count("█") > low.formatted_body.count("█")
    assert "95%" in high.body and "10%" in low.body
    assert "pattern" in high.formatted_body
    assert "failure" in low.formatted_body


def test_review_summary_counts_passes_and_failures() -> None:
    msg = format_review_summary(
        "demo",
        ProjectPhase.REVIEW,
        [
            {"task_id": "t1", "success": True, "description": "one"},
            {"task_id": "t2", "success": False, "description": "two"},
        ],
    )
    assert "1 passed" in msg.body
    assert "1 failed" in msg.body
    assert "<table>" in msg.formatted_body


def test_misc_helpers() -> None:
    assert "/agora" in format_help().body
    assert "📌" in format_note("@fabs:agora.local", "focus on errors").body
    assert format_ack("done").body.startswith("✓")
    assert format_error("bad input").body.startswith("⚠")


def test_formatted_message_to_content_keys() -> None:
    msg = format_help()
    content = msg.to_content()
    assert set(content.keys()) == {"msgtype", "body", "format", "formatted_body"}
