import pytest

from agora.core.agent import AgentConfig
from agora.core.errors import AgoraError
from agora.core.learning import Learning
from agora.core.project import PhaseChange
from agora.core.types import AgentRole, LearningCategory, ProjectPhase, TaskStatus
from agora.matrix.events import (
    AGENT_CONFIG_EVENT,
    KNOWLEDGE_REF_EVENT,
    LEARNING_EVENT,
    PHASE_CHANGE_EVENT,
    TASK_EVENT,
    TASK_RESULT_EVENT,
    agent_config_from_content,
    agent_config_to_content,
    is_agora_event,
    knowledge_ref_from_content,
    knowledge_ref_to_content,
    learning_from_content,
    learning_to_content,
    phase_change_from_content,
    phase_change_to_content,
    task_from_content,
    task_result_from_content,
    task_result_to_content,
    task_to_content,
)


def test_agent_config_roundtrip() -> None:
    cfg = AgentConfig(
        name="alice",
        role=AgentRole.ARCHITECT,
        model="claude-opus-4-6",
        instructions="design carefully",
        knowledge_files=("a.md", "b.md"),
    )
    out = agent_config_from_content(agent_config_to_content(cfg))
    assert out == cfg


def test_agent_config_missing_field_raises() -> None:
    with pytest.raises(AgoraError, match="required field"):
        agent_config_from_content({"role": "architect"})


def test_agent_config_invalid_role_raises() -> None:
    with pytest.raises(AgoraError, match="invalid agent_config"):
        agent_config_from_content({"name": "x", "role": "bogus"})


def test_task_event_roundtrip() -> None:
    content = task_to_content(
        task_id="t1",
        description="do a thing",
        agent_id="@a:agora.local",
        status=TaskStatus.ASSIGNED,
        fingerprint="deadbeef",
        depends_on=("t0",),
    )
    parsed = task_from_content(content)
    assert parsed["task_id"] == "t1"
    assert parsed["status"] == TaskStatus.ASSIGNED
    assert parsed["depends_on"] == ("t0",)
    assert parsed["fingerprint"] == "deadbeef"


def test_task_event_rejects_bad_status() -> None:
    content = task_to_content(
        task_id="t1", description="", agent_id=None, status=TaskStatus.PENDING, fingerprint="x"
    )
    content["status"] = "not-a-status"
    with pytest.raises(AgoraError):
        task_from_content(content)


def test_task_event_missing_required_field() -> None:
    with pytest.raises(AgoraError, match="missing required field"):
        task_from_content({"status": "pending"})


def test_task_result_roundtrip() -> None:
    content = task_result_to_content(
        task_id="t1",
        success=True,
        output="ok",
        artifacts=["file.py"],
        postcondition_results=[("tests_pass", True, "")],
    )
    parsed = task_result_from_content(content)
    assert parsed["success"] is True
    assert parsed["artifacts"] == ["file.py"]
    assert parsed["postcondition_results"] == [("tests_pass", True, "")]


def test_learning_event_roundtrip() -> None:
    learning = Learning(
        id="l1",
        category=LearningCategory.PATTERN,
        content="prefer DI",
        confidence=0.6,
        task_ref="t1",
        reinforcement_count=2,
        created_at="2026-04-15T00:00:00+00:00",
        last_reinforced_at="2026-04-15T01:00:00+00:00",
    )
    out = learning_from_content(learning_to_content(learning))
    assert out == learning


def test_learning_event_rejects_bad_category() -> None:
    with pytest.raises(AgoraError):
        learning_from_content(
            {
                "id": "l1",
                "category": "weird",
                "content": "x",
                "confidence": 0.5,
                "task_ref": "t1",
            }
        )


def test_phase_change_roundtrip() -> None:
    change = PhaseChange(
        from_phase=ProjectPhase.REVIEW,
        to_phase=ProjectPhase.IMPLEMENTATION,
        reason="needs fixes",
        timestamp="2026-04-15T00:00:00+00:00",
    )
    out = phase_change_from_content(phase_change_to_content(change))
    assert out == change


def test_knowledge_ref_roundtrip() -> None:
    content = knowledge_ref_to_content(
        mxc_uri="mxc://agora.local/abcd1234", filename="notes.md", description="design notes"
    )
    parsed = knowledge_ref_from_content(content)
    assert parsed["mxc_uri"] == "mxc://agora.local/abcd1234"
    assert parsed["filename"] == "notes.md"


def test_knowledge_ref_rejects_non_mxc_uri() -> None:
    with pytest.raises(AgoraError, match="mxc://"):
        knowledge_ref_to_content(mxc_uri="http://example.com/x", filename="x")


def test_learning_content_contains_no_floats() -> None:
    """Matrix canonical JSON forbids floats — Conduit panics on them.

    Confidence must be encoded as an integer basis-point field (``confidence_bp``).
    """
    learning = Learning(
        id="l1",
        category=LearningCategory.PATTERN,
        content="x",
        confidence=0.75,
        task_ref="t",
    )
    content = learning_to_content(learning)

    def _walk(value):
        if isinstance(value, dict):
            for v in value.values():
                yield from _walk(v)
        elif isinstance(value, list):
            for v in value:
                yield from _walk(v)
        else:
            yield value

    for v in _walk(content):
        assert not isinstance(v, float), f"float leaked into learning content: {v!r}"
    assert content["confidence_bp"] == 7500


def test_learning_confidence_bp_roundtrip_preserves_value() -> None:
    for conf in (0.0, 0.1, 0.25, 0.5, 0.8, 1.0):
        learning = Learning(
            id="l",
            category=LearningCategory.PATTERN,
            content="x",
            confidence=conf,
            task_ref="t",
        )
        out = learning_from_content(learning_to_content(learning))
        assert abs(out.confidence - conf) < 0.0001


def test_learning_from_content_accepts_legacy_float_confidence() -> None:
    """Events written before the int migration still parse correctly."""
    legacy = {
        "id": "old",
        "category": "pattern",
        "content": "x",
        "confidence": 0.7,
        "task_ref": "t",
    }
    out = learning_from_content(legacy)
    assert abs(out.confidence - 0.7) < 1e-9


def test_is_agora_event() -> None:
    assert is_agora_event(AGENT_CONFIG_EVENT)
    assert is_agora_event(TASK_EVENT)
    assert is_agora_event(TASK_RESULT_EVENT)
    assert is_agora_event(LEARNING_EVENT)
    assert is_agora_event(PHASE_CHANGE_EVENT)
    assert is_agora_event(KNOWLEDGE_REF_EVENT)
    assert not is_agora_event("m.room.message")
    assert not is_agora_event("")
