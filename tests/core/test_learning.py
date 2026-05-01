from agora.core.learning import (
    DECAY_RATE,
    MAX_CONFIDENCE,
    Learning,
    decay_learnings,
    filter_active,
    format_learnings_for_context,
    reinforce,
)
from agora.core.types import LearningCategory


def _learn(conf: float, cat: LearningCategory = LearningCategory.PATTERN, content: str = "x") -> Learning:
    return Learning(
        id=f"l-{conf}-{content}",
        category=cat,
        content=content,
        confidence=conf,
        task_ref="task-1",
    )


def test_decay_reduces_confidence() -> None:
    out = decay_learnings([_learn(0.8)])
    assert out[0].confidence == 0.8 - DECAY_RATE


def test_decay_does_not_go_below_zero() -> None:
    out = decay_learnings([_learn(0.05)])
    assert out[0].confidence == 0.0


def test_filter_active_removes_low_confidence() -> None:
    learnings = [_learn(0.9, content="keep"), _learn(0.1, content="drop")]
    active = filter_active(learnings)
    assert [l.content for l in active] == ["keep"]


def test_filter_active_sorts_by_confidence_desc() -> None:
    learnings = [_learn(0.5, content="mid"), _learn(0.9, content="high"), _learn(0.7, content="low-ish")]
    active = filter_active(learnings)
    assert [l.content for l in active] == ["high", "low-ish", "mid"]


def test_reinforce_boosts_confidence() -> None:
    before = _learn(0.5)
    after = reinforce(before)
    assert after.confidence > before.confidence
    assert after.reinforcement_count == 1
    assert after.last_reinforced_at


def test_reinforce_caps_at_one() -> None:
    after = reinforce(_learn(0.95))
    assert after.confidence == MAX_CONFIDENCE


def test_format_learnings_groups_by_category() -> None:
    learnings = [
        _learn(0.8, LearningCategory.PATTERN, content="use specs"),
        _learn(0.7, LearningCategory.FAILURE, content="avoid recursion"),
    ]
    text = format_learnings_for_context(learnings)
    assert "### pattern" in text
    assert "### failure" in text
    assert "use specs" in text
    assert "avoid recursion" in text
