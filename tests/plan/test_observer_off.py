"""The Matrix surface is optional: enable_observer=False must need no homeserver.

Guardrail tests for the C2 ruling (deployment-reconciliation, 2026-07-15).
run_phased drives whole lifecycles headless; requiring a live Conduit for rooms
nobody opens was a hard dependency for nothing. The named regression is
orchestrator.py:1093, which skipped only the dispatcher/renderer/review-
coordinator while still building a real client and creating rooms.
"""

from __future__ import annotations

import pytest

from agora.core.agent import AgentRole
from agora.fleet.inner_tools import ToolContext, get_tool_executor
from agora.matrix.client import NullMatrixClient
from agora.plan.harness import build_matrix_client
from tests.conftest import make_harness_config


class _ExplodingClient:
    """Any construction/use of a real client with the observer off is the bug."""

    def __init__(self, **_kw) -> None:
        raise AssertionError(
            "a REAL Matrix client was constructed with enable_observer=False "
            "(orchestrator.py:1093 regression)"
        )


@pytest.mark.asyncio
async def test_observer_off_builds_no_real_client(monkeypatch) -> None:
    """The named regression: no real client construction when the flag is off."""
    from dataclasses import replace

    monkeypatch.setattr("agora.plan.harness.AgoraMatrixClient", _ExplodingClient)
    cfg = replace(make_harness_config(), enable_observer=False)

    client = await build_matrix_client(cfg)

    assert isinstance(client, NullMatrixClient)


@pytest.mark.asyncio
async def test_observer_off_needs_no_password() -> None:
    """An unobserved run must not require a Matrix secret it never uses."""
    from dataclasses import replace

    cfg = replace(make_harness_config(), enable_observer=False, system_password="")
    client = await build_matrix_client(cfg)
    assert isinstance(client, NullMatrixClient)


def _ctx(tmp_path, *, live: bool) -> ToolContext:
    return ToolContext(
        work_dir=str(tmp_path),
        matrix_client=NullMatrixClient(),
        agent_room_id="!agent:local",
        project_room_id="!proj:local",
        matrix_live=live,
    )


@pytest.mark.asyncio
async def test_post_note_records_to_log_instead_of_claiming_delivery(tmp_path) -> None:
    """Informational tool: recorded, and HONEST that nobody saw it."""
    tools = get_tool_executor(AgentRole.IMPLEMENTER, _ctx(tmp_path, live=False))
    out = await tools["post_note"]({"body": "halfway done"})
    assert "recorded to log" in out
    assert "posted" not in out.replace("NOT delivered", "")


@pytest.mark.asyncio
async def test_report_progress_records_to_log(tmp_path) -> None:
    ctx = _ctx(tmp_path, live=False)
    tools = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await tools["report_progress"]({"message": "step 1 of 3"})
    assert "recorded to log" in out
    # Provenance is unconditional (F3): the entry is captured either way.
    assert ctx.progress_log == [{"message": "step 1 of 3"}]


@pytest.mark.asyncio
async def test_request_review_refuses_loudly(tmp_path) -> None:
    """Human-blocking tool: LOUD unavailable, never a silent 'review requested'."""
    ctx = _ctx(tmp_path, live=False)
    tools = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await tools["request_review"]({"summary": "please look"})
    assert out.startswith("ERROR:")
    assert "UNAVAILABLE" in out
    assert ctx.reviews_requested == [{"summary": "please look"}]  # still recorded


@pytest.mark.asyncio
async def test_await_user_decision_refuses_instead_of_blocking(tmp_path) -> None:
    """Must refuse BEFORE registering a future no human can ever resolve."""

    class _Control:
        pending_decisions: dict = {}

    ctx = _ctx(tmp_path, live=False)
    ctx.control = _Control()
    tools = get_tool_executor(AgentRole.IMPLEMENTER, ctx)
    out = await tools["await_user_decision"](
        {"question": "ship it?", "decision_id": "d1", "options": ["yes", "no"]}
    )
    assert out.startswith("ERROR:")
    assert "UNAVAILABLE" in out
    assert _Control.pending_decisions == {}, "must not register a future nobody can resolve"


def test_doctor_skip_is_not_a_green() -> None:
    """'skipped' is a third state: a check that never ran is not one that passed."""
    from agora.doctor import format_line, report, skipped

    r = skipped("conduit", "observer off")
    assert r.skipped and r.ok  # doesn't fail the gate...
    assert "[SKIP]" in format_line(r)  # ...but never claims to have verified
    lines: list[str] = []
    assert report([r], lines.append) == 0
    assert any("1 skipped" in line for line in lines)
