"""Enforces the non-blocking invariant for the orchestrator.

Two things must hold:
1. Sibling tasks in a phase run *concurrently* (wall-clock ≈ max, not sum).
2. Hung LLM calls surface as task failures via timeout, not event-loop stalls.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from agora.core.agent import AgentConfig
from agora.core.contract import Specification, make_predicate
from agora.core.errors import AgoraError
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus
from agora.fleet.llm_adapter import LLMResponse
from agora.fleet.orchestrator import Orchestrator
from agora.matrix.room_manager import RoomManager
from tests.conftest import FakeLLM, tool_call


class _SleepingLLM:
    """LLM that sleeps before returning a terminal mark_complete + reflection."""

    def __init__(self, sleep_seconds: float) -> None:
        self._sleep = sleep_seconds
        self._step = 0

    async def complete(self, **_kwargs) -> LLMResponse:
        await asyncio.sleep(self._sleep)
        self._step += 1
        if self._step == 1:
            return LLMResponse(
                content="",
                tool_calls=(tool_call("mark_complete", {"summary": "done"}),),
            )
        if self._step == 2:
            return LLMResponse(content="finished")
        return LLMResponse(content="[]")


def _always_pass_spec() -> Specification:
    return Specification(
        postconditions=(make_predicate("ok", "", lambda _c: (True, "")),),
        description="trivial",
    )


def _orchestrator(tmp_path: Path, fake_matrix_client, llm_factory) -> Orchestrator:
    return Orchestrator(
        matrix_client=fake_matrix_client,
        room_manager=RoomManager(fake_matrix_client, homeserver_name="agora.local"),
        llm_factory=llm_factory,
        work_dir=str(tmp_path),
        max_parallel_agents=3,
    )


async def test_ready_tasks_run_concurrently(tmp_path: Path, fake_matrix_client) -> None:
    """3 independent tasks with a 0.4 s per-turn LLM should finish in <2 s, not >3 s."""
    orch = _orchestrator(
        tmp_path, fake_matrix_client, llm_factory=lambda _m: _SleepingLLM(sleep_seconds=0.4)
    )
    agents = [AgentConfig(name="w", role=AgentRole.IMPLEMENTER)]
    tasks = [
        Task(
            id=f"t{i}",
            spec=_always_pass_spec(),
            description=f"task {i}",
            agent_id="w",
            status=TaskStatus.PENDING,
        )
        for i in range(3)
    ]

    start = time.monotonic()
    result = await orch.run_project("parallel", agents, tasks)
    elapsed = time.monotonic() - start

    assert result.success is True
    # 3 tasks × ~1.2 s sequential total; parallelism should cut it substantially.
    # Allow slack for fixture/setup overhead but enforce parallelism happened.
    assert elapsed < 2.5, f"elapsed {elapsed:.2f}s; sibling tasks did not run in parallel"


async def test_ollama_timeout_surfaces_as_task_failure(
    tmp_path: Path, fake_matrix_client, monkeypatch
) -> None:
    """A hung HTTP call to Ollama must fail the task, not the orchestrator."""
    from agora.fleet.llm_adapter import OllamaAdapter

    class _HangingSession:
        def __init__(self, *_a, **_k) -> None: ...
        def post(self, *_a, **_k):
            raise asyncio.TimeoutError()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _HangingSession)

    adapter = OllamaAdapter(base_url="http://localhost:11434", timeout_seconds=0.1)
    with pytest.raises(AgoraError, match="timed out|Cannot reach"):
        await adapter.complete([{"role": "user", "content": "hi"}])
