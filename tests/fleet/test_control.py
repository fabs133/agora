"""OrchestratorControl unit tests."""

from __future__ import annotations

import asyncio

import pytest

from agora.fleet.control import AbortedError, OrchestratorControl
from agora.observe.commands import parse_command


@pytest.fixture
async def setup(fake_matrix_client):
    room_id = await fake_matrix_client.create_room("proj")
    control = OrchestratorControl(project_room_id=room_id, matrix_client=fake_matrix_client)
    return control, fake_matrix_client, room_id


async def test_note_accumulates_and_is_drained_nondestructively(setup) -> None:
    control, client, room_id = setup
    cmd = parse_command("/agora note focus on errors", sender="@fabs:agora.local")
    assert cmd is not None
    await control.handle_command(room_id, cmd)
    assert "focus on errors" in control.notes

    # consume_notes returns a snapshot; notes persist so later turns still see them.
    snapshot = control.consume_notes()
    assert snapshot == ["focus on errors"]
    assert control.notes == ["focus on errors"]  # still present


async def test_pause_then_resume_gates_coroutine(setup) -> None:
    control, client, room_id = setup
    await control.handle_command(room_id, parse_command("/agora pause") or _fail())
    assert not control.pause_event.is_set()

    waiter = asyncio.create_task(control.wait_unpaused())
    await asyncio.sleep(0.02)
    assert not waiter.done()

    await control.handle_command(room_id, parse_command("/agora resume") or _fail())
    assert control.pause_event.is_set()
    await asyncio.wait_for(waiter, timeout=1)


async def test_abort_sets_state_and_raises(setup) -> None:
    control, client, room_id = setup
    await control.handle_command(
        room_id, parse_command("/agora abort tests failing") or _fail()
    )
    assert control.is_aborted()
    assert "tests failing" in control.abort_reason
    with pytest.raises(AbortedError):
        control.raise_if_aborted()


async def test_abort_wakes_paused_waiter(setup) -> None:
    control, client, room_id = setup
    await control.handle_command(room_id, parse_command("/agora pause") or _fail())
    waiter = asyncio.create_task(control.wait_unpaused())
    await asyncio.sleep(0.01)
    assert not waiter.done()

    await control.handle_command(room_id, parse_command("/agora abort") or _fail())
    await asyncio.wait_for(waiter, timeout=1)  # doesn't hang
    assert control.is_aborted()


async def test_redirect_is_one_shot(setup) -> None:
    control, client, room_id = setup
    await control.handle_command(
        room_id,
        parse_command('/agora redirect impl "focus on error handling"') or _fail(),
    )
    assert control.consume_redirect("impl") == "focus on error handling"
    # Second call pops nothing.
    assert control.consume_redirect("impl") is None


async def test_commands_for_other_rooms_are_ignored(setup) -> None:
    control, client, room_id = setup
    await control.handle_command(
        "!other:agora.local", parse_command("/agora pause") or _fail()
    )
    assert control.pause_event.is_set()  # still running


async def test_unknown_verb_emits_error_ack(setup) -> None:
    control, client, room_id = setup
    # `parse_command` folds unknown verbs to VERB_HELP; this exercises validate.
    cmd = parse_command("/agora note")
    assert cmd is not None
    await control.handle_command(room_id, cmd)
    # An ack/error message landed in the room timeline.
    tl = client.rooms[room_id].timeline
    assert any("required" in str(ev.content) or "note" in str(ev.content) for ev in tl)


async def test_review_verb_is_ignored(setup) -> None:
    """ReviewCoordinator owns /agora review; the control object ignores it."""
    control, client, room_id = setup
    before = len(client.rooms[room_id].timeline)
    await control.handle_command(
        room_id, parse_command("/agora review approve") or _fail()
    )
    assert len(client.rooms[room_id].timeline) == before


def _fail():
    raise AssertionError("parse_command returned None unexpectedly")
