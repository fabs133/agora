"""Tests for the Element-side interactive surfaces added in Round 10:

- Emoji reactions (``m.reaction``) parsed by the dispatcher and aggregated per task.
- Threaded replies (``m.in_reply_to``) routed as implicit task comments.
- Command-reference card posted at observer attach.
- Reaction counts surfaced in the review summary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.core.types import RoomId
from agora.fleet.control import OrchestratorControl
from agora.matrix.sync import (
    EventDispatcher,
    ReactionEvent,
    ReplyEvent,
    _extract_reply_target,
    _strip_matrix_reply_fallback,
)
from agora.observe.formatters import (
    ArtifactSnapshot,
    format_command_reference,
    format_review_summary,
    format_write_event,
)


# =============================================================================
# EventDispatcher — reactions + reply relations
# =============================================================================


async def test_dispatcher_routes_m_reaction_to_reaction_handlers() -> None:
    dispatcher = EventDispatcher()
    seen: list[ReactionEvent] = []

    async def on_react(room_id: RoomId, react: ReactionEvent) -> None:
        seen.append(react)

    dispatcher.on_reaction(on_react)

    await dispatcher.handle(
        "!room:ag",
        {
            "type": "m.reaction",
            "event_id": "$r1",
            "sender": "@fabs:ag",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$target1",
                    "key": "✅",
                }
            },
        },
    )
    assert len(seen) == 1
    assert seen[0].target_event_id == "$target1"
    assert seen[0].key == "✅"
    assert seen[0].sender == "@fabs:ag"


async def test_dispatcher_ignores_reactions_without_required_fields() -> None:
    dispatcher = EventDispatcher()
    seen: list[ReactionEvent] = []

    async def on_react(room_id: RoomId, react: ReactionEvent) -> None:
        seen.append(react)

    dispatcher.on_reaction(on_react)

    # Missing rel_type.
    await dispatcher.handle(
        "!r", {"type": "m.reaction", "event_id": "$a", "content": {"m.relates_to": {}}}
    )
    # Wrong rel_type.
    await dispatcher.handle(
        "!r",
        {
            "type": "m.reaction",
            "event_id": "$b",
            "content": {
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$t", "key": "👍"}
            },
        },
    )
    assert seen == []


async def test_dispatcher_routes_threaded_reply_to_reply_handlers() -> None:
    dispatcher = EventDispatcher()
    reply_seen: list[ReplyEvent] = []
    command_seen: list = []

    async def on_reply(room_id: RoomId, reply: ReplyEvent) -> None:
        reply_seen.append(reply)

    async def on_command(room_id: RoomId, cmd) -> None:
        command_seen.append(cmd)

    dispatcher.on_reply(on_reply)
    dispatcher.on_command(on_command)

    await dispatcher.handle(
        "!room",
        {
            "type": "m.room.message",
            "event_id": "$reply1",
            "sender": "@fabs:ag",
            "content": {
                "msgtype": "m.text",
                "body": "> <@agora:ag> ✎ bot.py — edit:insert_before\n\nuse random.randint from stdlib",
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": "$card1"},
                },
            },
        },
    )
    assert len(reply_seen) == 1
    assert reply_seen[0].target_event_id == "$card1"
    # Fallback quote stripped — body contains only the actual reply text.
    assert "random.randint" in reply_seen[0].body
    assert "✎ bot.py" not in reply_seen[0].body
    # Non-slash-command messages still pass through; parse_command returns None
    # and nothing is dispatched.
    assert command_seen == []


def test_extract_reply_target_pulls_in_reply_to_event_id() -> None:
    assert _extract_reply_target(
        {"m.relates_to": {"m.in_reply_to": {"event_id": "$x"}}}
    ) == "$x"
    assert _extract_reply_target({}) is None
    assert _extract_reply_target({"m.relates_to": {"m.in_reply_to": {}}}) is None


def test_strip_matrix_reply_fallback_removes_quoted_block() -> None:
    body = "> <@agora:ag> original first line\n> original second line\n\nmy reply"
    assert _strip_matrix_reply_fallback(body) == "my reply"


def test_strip_matrix_reply_fallback_returns_body_when_no_quote() -> None:
    assert _strip_matrix_reply_fallback("plain text") == "plain text"


# =============================================================================
# OrchestratorControl — task-card registry + reaction handling
# =============================================================================


async def test_control_registers_task_cards_and_resolves_them(fake_matrix_client) -> None:
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)

    control.register_task_card("$card_a", "build_ping")
    control.register_task_card("$card_b", "build_roll")

    assert control.resolve_task_from_event("$card_a") == "build_ping"
    assert control.resolve_task_from_event("$card_b") == "build_roll"
    assert control.resolve_task_from_event("$unknown") is None


async def test_control_handle_reaction_aggregates_per_task(fake_matrix_client) -> None:
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)
    control.register_task_card("$card_ping", "build_ping")

    react = ReactionEvent(
        event_id="$r1", target_event_id="$card_ping", key="✅", sender="@fabs:ag"
    )
    await control.handle_reaction(room, react)
    react2 = ReactionEvent(
        event_id="$r2", target_event_id="$card_ping", key="🔁", sender="@fabs:ag"
    )
    await control.handle_reaction(room, react2)
    assert control.task_reactions["build_ping"] == [
        ("@fabs:ag", "✅"),
        ("@fabs:ag", "🔁"),
    ]


async def test_control_handle_reaction_ignores_unknown_targets(fake_matrix_client) -> None:
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)
    react = ReactionEvent(
        event_id="$r", target_event_id="$not_a_card", key="✅", sender="@f:ag"
    )
    await control.handle_reaction(room, react)
    assert control.task_reactions == {}


async def test_control_handle_reaction_ignores_other_rooms(fake_matrix_client) -> None:
    room = await fake_matrix_client.create_room(name="project", topic="")
    other = await fake_matrix_client.create_room(name="other", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)
    control.register_task_card("$card", "build_ping")
    react = ReactionEvent(
        event_id="$r", target_event_id="$card", key="✅", sender="@f:ag"
    )
    await control.handle_reaction(other, react)
    assert control.task_reactions == {}


# =============================================================================
# Threaded replies → implicit task comments
# =============================================================================


async def test_control_reply_to_task_card_queues_a_task_comment(fake_matrix_client) -> None:
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)
    control.register_task_card("$card_roll", "build_roll")

    reply = ReplyEvent(
        event_id="$reply1",
        target_event_id="$card_roll",
        body="use random.randint from the stdlib",
        sender="@fabs:ag",
    )
    await control.handle_reply(room, reply)

    assert control.task_comments["build_roll"] == [
        "use random.randint from the stdlib"
    ]
    # An ack was posted.
    timeline = fake_matrix_client.rooms[room].timeline
    assert any(
        "comment linked to task 'build_roll'" in m.content.get("body", "")
        for m in timeline
    )


async def test_control_reply_to_unknown_card_is_ignored(fake_matrix_client) -> None:
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)
    reply = ReplyEvent(
        event_id="$r", target_event_id="$nope", body="x", sender="@f:ag"
    )
    await control.handle_reply(room, reply)
    assert control.task_comments == {}


async def test_control_reply_with_empty_body_is_ignored(fake_matrix_client) -> None:
    room = await fake_matrix_client.create_room(name="project", topic="")
    control = OrchestratorControl(project_room_id=room, matrix_client=fake_matrix_client)
    control.register_task_card("$card", "build_ping")
    reply = ReplyEvent(
        event_id="$r", target_event_id="$card", body="   ", sender="@f:ag"
    )
    await control.handle_reply(room, reply)
    assert control.task_comments == {}


# =============================================================================
# Formatter — reaction counts in review summary, command-reference card
# =============================================================================


def test_review_summary_includes_reaction_counts() -> None:
    snapshot = ArtifactSnapshot(
        files=[],
        recent_commits=[],
        postcondition_failures=[],
        reaction_counts={"build_roll": {"🔁": 2, "✅": 1}, "build_ping": {"✅": 1}},
    )
    msg = format_review_summary(
        project_name="p",
        phase=__import__("agora.core.types", fromlist=["ProjectPhase"]).ProjectPhase.REVIEW,
        task_results_summary=[],
        artifact=snapshot,
    )
    assert "build_roll" in msg.body
    assert "🔁" in msg.body
    assert "×2" in msg.body or "2" in msg.body
    assert "Reviewer signal" in msg.formatted_body


def test_format_command_reference_has_reactions_and_replies_sections() -> None:
    msg = format_command_reference()
    assert "✅" in msg.body and "🔁" in msg.body and "💬" in msg.body
    assert "Per-task" in msg.formatted_body
    assert "reply to a task card" in msg.formatted_body.lower()
    # Collapsed by default (Element renders <details>).
    assert "<details>" in msg.formatted_body


def test_format_write_event_includes_reaction_hint_line() -> None:
    msg = format_write_event(
        task_id="t",
        path="x.py",
        operation="write",
        size_bytes=10,
        hook_summary=[("check_python", True)],
    )
    assert "✅" in msg.body and "🔁" in msg.body and "💬" in msg.body
    assert "react" in msg.body.lower()
