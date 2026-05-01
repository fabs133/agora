"""Unit test for the ``await_user_decision`` inner tool.

Doesn't touch real Matrix — uses a fake client + a real
:class:`OrchestratorControl` so the future-based blocking primitive is exercised
end-to-end.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agora.core.types import AgentRole
from agora.fleet.control import OrchestratorControl
from agora.fleet.inner_tools import ToolContext, get_tool_executor
# POLL_DECISION_ID_KEY / POLL_TAG_KEY were removed from event content in
# favour of a client-side poll_event_id → decision_id map. These tests no
# longer assert anything about custom wire keys.


class _FakeMatrixClient:
    """Minimal stand-in for MatrixClientProtocol — only ``send_event`` is used."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def send_event(self, room_id: str, event_type: str, content: dict) -> str:
        self.sent.append((room_id, event_type, content))
        return f"$poll_event_{len(self.sent)}"


def _make_ctx() -> tuple[ToolContext, OrchestratorControl, _FakeMatrixClient]:
    client = _FakeMatrixClient()
    control = OrchestratorControl(project_room_id="!room:x", matrix_client=client)  # type: ignore[arg-type]
    ctx = ToolContext(
        work_dir="/tmp",
        matrix_client=client,  # type: ignore[arg-type]
        agent_room_id="!agent:x",
        project_room_id="!room:x",
        control=control,
    )
    return ctx, control, client


async def test_decision_resolves_when_user_clicks():
    ctx, control, client = _make_ctx()
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    tool = executor["await_user_decision"]

    async def _simulate_click():
        # Give the tool a tick to post the fallback note + poll and register
        # the future (tool sends TWO events: plain m.room.message then poll).
        await asyncio.sleep(0.01)
        assert len(client.sent) >= 2, "tool should send fallback + poll"
        assert client.sent[0][1] == "m.room.message"  # fallback note first
        assert client.sent[1][1] == "m.poll.start"    # then the poll itself
        # Poll is the 2nd event; its event_id is $poll_event_2 per fake.
        poll_event_id = "$poll_event_2"
        decision_id = control.decision_id_for_poll(poll_event_id)
        assert decision_id == "storage"
        control.resolve_decision(decision_id, "sqlite")

    # Run the tool + click simulation concurrently.
    results = await asyncio.gather(
        tool(
            {
                "question": "Which storage backend?",
                "decision_id": "storage",
                "options": ["json", "sqlite"],
                "timeout_seconds": 2.0,
            }
        ),
        _simulate_click(),
    )
    assert results[0] == "sqlite"
    assert control.decision_responses["storage"] == "sqlite"


async def test_decision_times_out_if_no_click():
    ctx, control, _ = _make_ctx()
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    tool = executor["await_user_decision"]

    # No click simulated — the tool should surface a timeout error string.
    result = await tool(
        {
            "question": "Which one?",
            "decision_id": "never",
            "options": ["a", "b"],
            "timeout_seconds": 0.1,
        }
    )
    assert result.startswith("ERROR: decision 'never' timed out"), result


async def test_decision_rejects_short_options():
    ctx, _, _ = _make_ctx()
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    tool = executor["await_user_decision"]
    result = await tool(
        {
            "question": "Pick something",
            "decision_id": "x",
            "options": ["only_one"],
        }
    )
    assert "at least 2" in result


async def test_agora_decision_command_resolves_single_pending():
    """The `/agora decision <answer>` fallback command resolves the only
    pending decision without the user needing to know the decision_id."""
    from agora.observe.commands import parse_command

    ctx, control, client = _make_ctx()
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    tool = executor["await_user_decision"]

    async def _resolve_via_command():
        # Tool hasn't posted yet — give it a tick.
        await asyncio.sleep(0.01)
        # User types `/agora decision sqlite`; the command handler finds the
        # single pending decision and calls resolve_decision.
        cmd = parse_command("/agora decision sqlite", sender="@u:x")
        await control.handle_command("!room:x", cmd)

    result, _ = await asyncio.gather(
        tool(
            {
                "question": "?",
                "decision_id": "storage",
                "options": ["json", "sqlite"],
                "timeout_seconds": 2.0,
            }
        ),
        _resolve_via_command(),
    )
    assert result == "sqlite"


async def test_decision_without_control_returns_error():
    client = _FakeMatrixClient()
    ctx = ToolContext(
        work_dir="/tmp",
        matrix_client=client,  # type: ignore[arg-type]
        agent_room_id="!agent:x",
        project_room_id="!room:x",
        control=None,  # no observer → tool should degrade gracefully
    )
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    tool = executor["await_user_decision"]
    result = await tool(
        {
            "question": "?",
            "decision_id": "d",
            "options": ["a", "b"],
        }
    )
    assert result.startswith("ERROR: await_user_decision requires an observer")


async def test_decision_poll_has_decision_tag_and_id():
    ctx, control, client = _make_ctx()
    executor = get_tool_executor(AgentRole.ARCHITECT, ctx)
    tool = executor["await_user_decision"]

    async def _resolve():
        await asyncio.sleep(0.01)
        # Poll is the 2nd event (index 1): first is the fallback plain message,
        # then the poll itself.
        poll_id = "$poll_event_2"
        decision_id = control.decision_id_for_poll(poll_id)
        control.resolve_decision(decision_id, "foo")

    await asyncio.gather(
        tool(
            {
                "question": "Q?",
                "decision_id": "my_decision",
                "options": ["foo", "bar", "baz"],
                "timeout_seconds": 2.0,
            }
        ),
        _resolve(),
    )
    assert len(client.sent) == 2  # fallback note + poll
    fb_room, fb_type, fb_content = client.sent[0]
    assert fb_type == "m.room.message"
    assert "my_decision" in fb_content["body"]
    assert "/agora decision my_decision" in fb_content["body"]

    _, event_type, content = client.sent[1]
    assert event_type == "m.poll.start"
    assert control.decision_id_for_poll("$poll_event_2") == "my_decision"
    # New MSC3381 stable schema: 3 answers under m.poll, each with m.id + m.text array.
    assert len(content["m.poll"]["answers"]) == 3
    for answer in content["m.poll"]["answers"]:
        assert "m.id" in answer
        assert isinstance(answer["m.text"], list)
    assert content["m.poll"]["kind"] == "m.disclosed"
    # Unstable namespace dropped on SEND.
    assert "org.matrix.msc3381.poll.start" not in content
    # Top-level keys: only m.poll + m.text (fallback). No legacy body/msgtype.
    allowed_keys = {"m.poll", "m.text"}
    assert set(content.keys()) == allowed_keys, (
        f"unexpected extra keys on poll event: {set(content.keys()) - allowed_keys}"
    )
