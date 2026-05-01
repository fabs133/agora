from agora.core.types import ProjectPhase
from agora.observe.polls import (
    ANSWER_APPROVE,
    ANSWER_REJECT_ARCHITECTURE,
    POLL_RESPONSE_TYPE,
    POLL_RESPONSE_TYPE_UNSTABLE,
    POLL_START_TYPE,
    build_review_poll,
    parse_poll_response,
)


def test_build_review_poll_shape() -> None:
    """Asserts the v2.1 MSC3381-compliant schema.

    The stable schema: answer ids use the ``m.id`` field (not ``id``), text
    blocks are arrays of ``{mimetype, body}`` variants, ``kind`` is
    ``m.disclosed``, and the top-level fallback is a ``m.text`` array rather
    than the legacy ``body``+``msgtype`` pair. No unstable-namespace duplicate.
    """
    content = build_review_poll("demo", ProjectPhase.REVIEW)
    assert "m.poll" in content
    poll = content["m.poll"]
    assert poll["kind"] == "m.disclosed"
    assert poll["max_selections"] == 1
    # Answers use m.id + m.text arrays per spec.
    answer_ids = {a["m.id"] for a in poll["answers"]}
    assert ANSWER_APPROVE in answer_ids
    assert ANSWER_REJECT_ARCHITECTURE in answer_ids
    for answer in poll["answers"]:
        assert isinstance(answer["m.text"], list)
        assert answer["m.text"][0]["mimetype"] == "text/plain"
        assert answer["m.text"][0]["body"]
    # Question uses m.text array too.
    assert poll["question"]["m.text"][0]["mimetype"] == "text/plain"
    # Top-level fallback is an m.text array (not body/msgtype).
    assert isinstance(content["m.text"], list)
    fallback_body = content["m.text"][0]["body"]
    assert "Approve" in fallback_body
    # Unstable namespace dropped on SEND (parse still supports it for responses).
    assert "org.matrix.msc3381.poll.start" not in content
    # Legacy body/msgtype replaced by m.text array.
    assert "body" not in content
    assert "msgtype" not in content


def test_poll_start_event_type() -> None:
    from agora.observe.polls import review_poll_event_type

    assert review_poll_event_type() == POLL_START_TYPE


def test_parse_poll_response_stable_namespace() -> None:
    event = {
        "type": POLL_RESPONSE_TYPE,
        "event_id": "$resp1",
        "sender": "@fabs:agora.local",
        "content": {
            "m.relates_to": {"rel_type": "m.reference", "event_id": "$poll1"},
            "m.selections": [ANSWER_APPROVE],
        },
    }
    parsed = parse_poll_response(event)
    assert parsed is not None
    assert parsed.poll_event_id == "$poll1"
    assert parsed.user_id == "@fabs:agora.local"
    assert parsed.answer_id == ANSWER_APPROVE


def test_parse_poll_response_unstable_namespace() -> None:
    event = {
        "type": POLL_RESPONSE_TYPE_UNSTABLE,
        "event_id": "$resp2",
        "sender": "@fabs:agora.local",
        "content": {
            "m.relates_to": {"event_id": "$poll2"},
            "org.matrix.msc3381.poll.response": {"answers": [ANSWER_REJECT_ARCHITECTURE]},
        },
    }
    parsed = parse_poll_response(event)
    assert parsed is not None
    assert parsed.answer_id == ANSWER_REJECT_ARCHITECTURE


def test_parse_poll_response_ignores_non_poll_events() -> None:
    assert parse_poll_response({"type": "m.room.message", "content": {"body": "hi"}}) is None
    assert parse_poll_response({}) is None
    assert parse_poll_response("not a dict") is None  # type: ignore[arg-type]


def test_parse_poll_response_drops_malformed() -> None:
    # Missing relates_to
    assert (
        parse_poll_response(
            {
                "type": POLL_RESPONSE_TYPE,
                "content": {"m.selections": ["x"]},
            }
        )
        is None
    )
    # Missing answers
    assert (
        parse_poll_response(
            {
                "type": POLL_RESPONSE_TYPE,
                "content": {"m.relates_to": {"event_id": "$p"}},
            }
        )
        is None
    )
