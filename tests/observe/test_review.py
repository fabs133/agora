import asyncio

import pytest

from agora.core.project import Project
from agora.core.types import ProjectPhase
from agora.matrix.sync import EventDispatcher
from agora.observe.polls import (
    ANSWER_APPROVE,
    ANSWER_REJECT_ARCHITECTURE,
    POLL_RESPONSE_TYPE,
)
from agora.observe.review import ReviewCoordinator


def _project() -> Project:
    return Project(
        id="proj-1",
        name="demo",
        phase=ProjectPhase.REVIEW,
    )


async def test_approve_returns_approved_decision(fake_matrix_client) -> None:
    dispatcher = EventDispatcher()
    room_id = await fake_matrix_client.create_room("proj")
    coord = ReviewCoordinator(fake_matrix_client, dispatcher, room_id, timeout_seconds=5)

    async def _run():
        return await coord.request_review(_project(), [{"task_id": "t1", "success": True}])

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.02)  # let coordinator post summary + poll

    # The poll is the last event in the room's timeline. Its id is what observers reply to.
    poll_event = fake_matrix_client.rooms[room_id].timeline[-1]
    await dispatcher.handle(
        room_id,
        {
            "type": POLL_RESPONSE_TYPE,
            "event_id": "$resp-approve",
            "sender": "@fabs:agora.local",
            "content": {
                "m.relates_to": {"event_id": poll_event.event_id},
                "m.selections": [ANSWER_APPROVE],
            },
        },
    )

    decision = await asyncio.wait_for(task, timeout=2)
    assert decision.approved is True
    assert "approved" in decision.feedback.lower()


async def test_reject_loops_back_to_architecture(fake_matrix_client) -> None:
    dispatcher = EventDispatcher()
    room_id = await fake_matrix_client.create_room("proj")
    coord = ReviewCoordinator(fake_matrix_client, dispatcher, room_id, timeout_seconds=5)

    async def _run():
        return await coord.request_review(_project(), [])

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.02)

    poll_event = fake_matrix_client.rooms[room_id].timeline[-1]
    await dispatcher.handle(
        room_id,
        {
            "type": POLL_RESPONSE_TYPE,
            "event_id": "$resp-reject",
            "sender": "@reviewer:agora.local",
            "content": {
                "m.relates_to": {"event_id": poll_event.event_id},
                "m.selections": [ANSWER_REJECT_ARCHITECTURE],
            },
        },
    )

    decision = await asyncio.wait_for(task, timeout=2)
    assert decision.approved is False
    assert decision.return_to_phase == ProjectPhase.ARCHITECTURE


async def test_timeout_falls_back_to_auto_review(fake_matrix_client) -> None:
    dispatcher = EventDispatcher()
    room_id = await fake_matrix_client.create_room("proj")
    coord = ReviewCoordinator(fake_matrix_client, dispatcher, room_id, timeout_seconds=0.1)

    decision = await coord.request_review(
        _project(), [{"task_id": "t1", "success": False}]
    )
    assert decision.approved is False
    assert "timeout" in decision.feedback.lower()
    assert decision.return_to_phase == ProjectPhase.IMPLEMENTATION


async def test_command_fallback_vote(fake_matrix_client) -> None:
    """Users without a poll-aware client can vote via /agora review <id>."""
    dispatcher = EventDispatcher()
    room_id = await fake_matrix_client.create_room("proj")
    coord = ReviewCoordinator(fake_matrix_client, dispatcher, room_id, timeout_seconds=5)

    async def _run():
        return await coord.request_review(_project(), [])

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.02)

    await dispatcher.handle(
        room_id,
        {
            "type": "m.room.message",
            "event_id": "$cmd1",
            "sender": "@fabs:agora.local",
            "content": {"body": f"/agora review {ANSWER_APPROVE}", "msgtype": "m.text"},
        },
    )

    decision = await asyncio.wait_for(task, timeout=2)
    assert decision.approved is True
