"""End-to-end integration tests against a live Conduit homeserver.

Gated by ``AGORA_E2E=1`` — skipped by default so unit runs stay offline.

The LLM is still a :class:`FakeLLM` so we don't depend on a running Ollama; the
point of these tests is to exercise the *Matrix* + *orchestrator* + *observer*
stack end-to-end. Each test registers a throwaway user via the registration
token and tears itself down with the login session.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import aiohttp
import pytest

from agora.core.agent import AgentConfig
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole, ProjectPhase, TaskStatus
from agora.fleet.llm_adapter import LLMResponse
from agora.fleet.orchestrator import Orchestrator
from agora.matrix.client import AgoraMatrixClient
from agora.matrix.room_manager import RoomManager
from tests.conftest import FakeLLM, tool_call

pytestmark = pytest.mark.skipif(
    os.getenv("AGORA_E2E") != "1",
    reason="live e2e — set AGORA_E2E=1 and have Conduit running",
)

HOMESERVER = os.getenv("AGORA_MATRIX_HOMESERVER", "http://localhost:6167")
SERVER_NAME = os.getenv("AGORA_MATRIX_SERVER_NAME", "agora.local")
TOKEN = os.getenv("AGORA_MATRIX_TOKEN", "dev_only_CHANGE_ME")


def _always_pass() -> Specification:
    return Specification(
        postconditions=(make_predicate("ok", "", lambda _c: (True, "")),),
        description="trivial",
    )


def _plan_factory():
    return FakeLLM(
        [
            LLMResponse(
                content="",
                tool_calls=(tool_call("mark_complete", {"summary": "done"}),),
            ),
            LLMResponse(content="done"),
            LLMResponse(content="[]"),
        ]
        * 20
    )


async def _register(username: str, password: str) -> None:
    url = f"{HOMESERVER}/_matrix/client/v3/register"
    payload = {
        "auth": {"type": "m.login.registration_token", "token": TOKEN},
        "username": username,
        "password": password,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status in (200, 400):  # 400 = M_USER_IN_USE on re-run
                return
            text = await resp.text()
            raise RuntimeError(f"register failed ({resp.status}): {text}")


async def _make_client(username: str, password: str) -> AgoraMatrixClient:
    await _register(username, password)
    client = AgoraMatrixClient(
        homeserver=HOMESERVER, user_id=f"@{username}:{SERVER_NAME}"
    )
    await client.login(password)
    return client


# ------------------------------------------------------------------- Test 1: solo


async def test_solo_flow_end_to_end(tmp_path: Path) -> None:
    """Run the solo built-in flow against live Conduit; assert the project reaches DONE."""
    username = f"sys_{uuid.uuid4().hex[:8]}"
    password = "e2e-pass-1234"
    client = await _make_client(username, password)
    try:
        orch = Orchestrator(
            matrix_client=client,
            room_manager=RoomManager(client, homeserver_name=SERVER_NAME),
            llm_factory=lambda _m: _plan_factory(),
            work_dir=str(tmp_path / "work"),
            homeserver_name=SERVER_NAME,
            max_parallel_agents=1,
            enable_observer=True,
            skip_warmup=True,
            review_timeout_seconds=1.0,
            repo_root=str(tmp_path / "repos"),
        )
        result = await asyncio.wait_for(
            orch.run_project(
                "e2e-solo",
                [AgentConfig(name="impl", role=AgentRole.IMPLEMENTER)],
                [Task(id="t1", spec=_always_pass(), description="do it", agent_id="impl")],
            ),
            timeout=60,
        )
        assert result.success is True
        assert result.project.phase == ProjectPhase.DONE
        # The project room exists on Conduit and contains phase-change events.
        state = await client.get_room_state(result.project_room_id)
        types = {ev.get("type") for ev in state}
        assert "m.room.name" in types
    finally:
        await client.close()


# ------------------------------------------------- Test 2: architect-implementer


async def test_architect_implementer_end_to_end(tmp_path: Path) -> None:
    username = f"sys_{uuid.uuid4().hex[:8]}"
    password = "e2e-pass-1234"
    client = await _make_client(username, password)
    try:
        orch = Orchestrator(
            matrix_client=client,
            room_manager=RoomManager(client, homeserver_name=SERVER_NAME),
            llm_factory=lambda _m: _plan_factory(),
            work_dir=str(tmp_path / "work"),
            homeserver_name=SERVER_NAME,
            max_parallel_agents=2,
            enable_observer=True,
            skip_warmup=True,
            review_timeout_seconds=1.0,
            repo_root=str(tmp_path / "repos"),
        )
        agents = [
            AgentConfig(name="architect", role=AgentRole.ARCHITECT),
            AgentConfig(name="impl", role=AgentRole.IMPLEMENTER),
        ]
        tasks = [
            Task(
                id="design",
                spec=_always_pass(),
                description="design it",
                agent_id="architect",
                status=TaskStatus.PENDING,
            ),
            Task(
                id="build",
                spec=_always_pass(),
                description="build it",
                agent_id="impl",
                depends_on=("design",),
                status=TaskStatus.PENDING,
            ),
        ]
        result = await asyncio.wait_for(
            orch.run_project("e2e-arch-impl", agents, tasks), timeout=120
        )
        assert result.success is True
        assert result.project.phase == ProjectPhase.DONE
        # Both task results recorded.
        assert len(result.task_results) == 2
        assert all(r.success for r in result.task_results)
    finally:
        await client.close()
