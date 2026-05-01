"""Envelope wrap/unwrap for homeservers (Conduit) that reject custom timeline types."""

from __future__ import annotations

from agora.matrix.client import (
    AGORA_ENVELOPE_DATA_KEY,
    AGORA_ENVELOPE_TYPE_KEY,
    unwrap_envelope,
    wrap_for_homeserver,
)
from agora.matrix.events import LEARNING_EVENT, TASK_EVENT


def test_wraps_custom_type_as_room_message() -> None:
    content = {"task_id": "t1", "status": "pending"}
    wire_type, wire_content = wrap_for_homeserver(TASK_EVENT, content)
    assert wire_type == "m.room.message"
    assert wire_content["msgtype"] == "m.notice"
    assert wire_content[AGORA_ENVELOPE_TYPE_KEY] == TASK_EVENT
    assert wire_content[AGORA_ENVELOPE_DATA_KEY] == content
    # Fallback body is non-empty so raw clients show something readable.
    assert "t1" in wire_content["body"]


def test_passes_room_events_through() -> None:
    wire_type, wire_content = wrap_for_homeserver(
        "m.room.message", {"body": "hi", "msgtype": "m.text"}
    )
    assert wire_type == "m.room.message"
    assert wire_content == {"body": "hi", "msgtype": "m.text"}


def test_passes_poll_events_through() -> None:
    """m.poll.start must NOT be wrapped — Element needs the native type to render polls."""
    poll_content = {
        "m.poll": {"question": {"m.text": "?"}, "answers": [], "kind": "m.poll.disclosed"}
    }
    wire_type, wire_content = wrap_for_homeserver("m.poll.start", poll_content)
    assert wire_type == "m.poll.start"
    assert wire_content == poll_content
    # Also the response events.
    rt, rc = wrap_for_homeserver("m.poll.response", {"m.selections": ["approve"]})
    assert rt == "m.poll.response"
    assert rc == {"m.selections": ["approve"]}


def test_passes_other_native_matrix_events_through() -> None:
    """m.reaction, m.space.*, etc. are Matrix spec and should pass through."""
    for etype in ("m.reaction", "m.space.child", "m.space.parent"):
        wt, _ = wrap_for_homeserver(etype, {})
        assert wt == etype, f"{etype} got wrapped unexpectedly"


def test_unwrap_restores_original_type_and_content() -> None:
    content = {"category": "pattern", "content": "use DI", "confidence": 0.7}
    wire_type, wire_content = wrap_for_homeserver(LEARNING_EVENT, content)
    event = {
        "type": wire_type,
        "event_id": "$abc",
        "sender": "@a:agora.local",
        "content": wire_content,
    }
    unwrapped = unwrap_envelope(event)
    assert unwrapped["type"] == LEARNING_EVENT
    assert unwrapped["content"] == content
    # Event id is preserved so dedup still works.
    assert unwrapped["event_id"] == "$abc"


def test_unwrap_passes_through_plain_message() -> None:
    event = {
        "type": "m.room.message",
        "event_id": "$plain",
        "content": {"body": "hello", "msgtype": "m.text"},
    }
    assert unwrap_envelope(event) == event


def test_unwrap_passes_through_non_dict() -> None:
    assert unwrap_envelope(None) is None  # type: ignore[arg-type]
    assert unwrap_envelope("not a dict") == "not a dict"  # type: ignore[arg-type]


def test_roundtrip_for_every_agora_event() -> None:
    from agora.matrix.events import (
        AGENT_CONFIG_EVENT,
        KNOWLEDGE_REF_EVENT,
        PHASE_CHANGE_EVENT,
        TASK_RESULT_EVENT,
    )

    for etype in (
        AGENT_CONFIG_EVENT,
        TASK_EVENT,
        TASK_RESULT_EVENT,
        LEARNING_EVENT,
        PHASE_CHANGE_EVENT,
        KNOWLEDGE_REF_EVENT,
    ):
        original = {"probe": etype}
        wt, wc = wrap_for_homeserver(etype, original)
        unwrapped = unwrap_envelope({"type": wt, "content": wc, "event_id": "$x"})
        assert unwrapped["type"] == etype
        assert unwrapped["content"] == original
