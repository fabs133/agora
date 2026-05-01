"""Matrix polls (MSC3381) — construction and response parsing.

Element ships first-class UI for MSC3381 polls. We post ``m.poll.start`` with a
disclosed kind and a small list of options keyed by stable answer ids. Observers
click an option in Element; Element emits ``m.poll.response`` events that we
parse back into :class:`PollResponse` structs.

Both the stable namespace (``m.poll.*``) and the unstable MSC3381 v1 namespace
(``org.matrix.msc3381.poll.*``) are handled, because older Element builds still
emit the unstable form.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from agora.core.types import EventId, ProjectPhase

# Stable namespace
POLL_START_TYPE = "m.poll.start"
POLL_RESPONSE_TYPE = "m.poll.response"
POLL_END_TYPE = "m.poll.end"

# Unstable MSC3381 v1 namespace (Element's older path)
POLL_START_TYPE_UNSTABLE = "org.matrix.msc3381.poll.start"
POLL_RESPONSE_TYPE_UNSTABLE = "org.matrix.msc3381.poll.response"

# Answer ids used across the review workflow. Stable so parsers never drift.
ANSWER_APPROVE = "approve"
ANSWER_REJECT_ANALYSIS = "reject_analysis"
ANSWER_REJECT_ARCHITECTURE = "reject_architecture"
ANSWER_REJECT_IMPLEMENTATION = "reject_implementation"
ANSWER_REJECT_TESTING = "reject_testing"

_REVIEW_OPTIONS: list[tuple[str, str]] = [
    (ANSWER_APPROVE, "✅ Approve — ship it"),
    (ANSWER_REJECT_ANALYSIS, "📝 Reject → rework ANALYSIS"),
    (ANSWER_REJECT_ARCHITECTURE, "🏗️ Reject → rework ARCHITECTURE"),
    (ANSWER_REJECT_IMPLEMENTATION, "🔧 Reject → rework IMPLEMENTATION"),
    (ANSWER_REJECT_TESTING, "🧪 Reject → rework TESTING"),
]


@dataclass(frozen=True)
class PollResponse:
    """Parsed ``m.poll.response`` event."""

    poll_event_id: EventId
    user_id: str
    answer_id: str
    raw_event_id: EventId


# Constants kept for backward compatibility with callers that imported them
# in earlier revisions. No longer emitted on the wire — Element was dropping
# poll events with extra top-level keys, and the planner's decision routing
# works purely off the poll_event_id → decision_id mapping maintained in
# :class:`agora.fleet.control.OrchestratorControl`.
POLL_TAG_KEY = "org.matrix.agora.poll_tag"
POLL_DECISION_ID_KEY = "org.matrix.agora.decision_id"
POLL_TAG_REVIEW = "review"
POLL_TAG_DECISION = "decision"


def build_poll(
    question: str,
    options: list[tuple[str, str]],
    *,
    fallback_verb: str = "review",
) -> dict:
    """Construct an MSC3381 stable-schema poll event content.

    ``options`` is a list of ``(answer_id, label)`` tuples. ``fallback_verb``
    controls the plaintext ``/agora <verb> <id>`` hint embedded in the
    top-level ``m.text`` fallback block (for clients without poll UI).

    Conforms to the MSC3381 stable schema: answer ids use the ``m.id`` field,
    text blocks use ``m.text`` arrays with ``{mimetype, body}`` variants, kind
    is ``m.disclosed``, and the top-level fallback is an ``m.text`` array (not
    a ``body``+``msgtype`` pair). The unstable ``org.matrix.msc3381.poll.start``
    dual-emit is NOT produced — current Element ignores it and earlier drafts
    of our own emitter included non-spec custom keys that caused Element to
    render empty bubbles.
    """
    return {
        "m.poll": {
            "question": {
                "m.text": [{"mimetype": "text/plain", "body": question}],
            },
            "kind": "m.disclosed",
            "max_selections": 1,
            "answers": [
                {
                    "m.id": answer_id,
                    "m.text": [{"mimetype": "text/plain", "body": label}],
                }
                for answer_id, label in options
            ],
        },
        "m.text": [
            {
                "mimetype": "text/plain",
                "body": _fallback_body(question, options, fallback_verb),
            }
        ],
    }


def build_review_poll(
    project_name: str,
    current_phase: ProjectPhase,
) -> dict:
    """Produce the ``content`` payload for a review poll (5 fixed options)."""
    question = f"Review for '{project_name}' (finished in {current_phase.value}) — pick one:"
    return build_poll(question, _REVIEW_OPTIONS, fallback_verb="review")


def build_decision_poll(
    question: str,
    options: list[tuple[str, str]],
    decision_id: str,  # retained for API stability; stored client-side only
) -> dict:
    """Poll used by the planner to ask the user a blocking design question.

    ``decision_id`` is NOT stamped into the event content — some Element
    builds reject poll events with unknown top-level keys. Instead the
    tool-side code maintains a ``poll_event_id → decision_id`` map in the
    :class:`OrchestratorControl`, so responses route cleanly without
    polluting the wire event.
    """
    return build_poll(question, options, fallback_verb="decision")


def _fallback_body(
    question: str,
    options: list[tuple[str, str]],
    verb: str = "review",
) -> str:
    lines = [question, ""]
    for answer_id, label in options:
        lines.append(f"  • {label}    (id: {answer_id})")
    lines.append("")
    lines.append(
        f"Reply with `/agora {verb} <id>` if your Matrix client cannot render polls."
    )
    return "\n".join(lines)


def parse_poll_response(event: dict) -> PollResponse | None:
    """Parse an ``m.poll.response`` (or unstable equivalent). Returns ``None`` if
    the event is malformed or not actually a poll response.
    """
    if not isinstance(event, dict):
        return None
    etype = event.get("type")
    if etype not in {POLL_RESPONSE_TYPE, POLL_RESPONSE_TYPE_UNSTABLE}:
        return None
    content = event.get("content") or {}

    poll_event_id = _extract_relates_to(content)
    answer_id = _extract_answer_id(content)
    if not poll_event_id or not answer_id:
        return None

    return PollResponse(
        poll_event_id=poll_event_id,
        user_id=str(event.get("sender", "")),
        answer_id=answer_id,
        raw_event_id=str(event.get("event_id", "")),
    )


def _extract_relates_to(content: dict) -> str | None:
    """Find the target poll event id under ``m.relates_to`` (both namespaces)."""
    relates = content.get("m.relates_to") or content.get(
        "org.matrix.msc3381.poll.response.relates_to"
    )
    if isinstance(relates, dict):
        event_id = relates.get("event_id")
        if isinstance(event_id, str):
            return event_id
    return None


def _extract_answer_id(content: dict) -> str | None:
    """Find the selected answer id under either namespace."""
    for key in ("m.selections", "org.matrix.msc3381.poll.response"):
        section = content.get(key)
        if isinstance(section, dict):
            # MSC3381 v1 nests "answers" inside this dict.
            answers = section.get("answers")
            if isinstance(answers, list) and answers:
                first = answers[0]
                if isinstance(first, str):
                    return first
        if isinstance(section, list) and section:
            first = section[0]
            if isinstance(first, str):
                return first
    return None


def review_poll_event_type() -> str:
    """The Matrix event type to use when *sending* the poll."""
    return POLL_START_TYPE


def new_poll_fallback_event_id() -> EventId:
    """For tests / fakes that need a placeholder poll event id."""
    return f"${uuid.uuid4().hex[:16]}"
