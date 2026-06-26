"""Observer tests: low-level emit, derivation from results, and an
end-to-end emission through the real Orchestrator with a scripted LLM."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agora.core.agent import AgentConfig
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus
from agora.fleet.llm_adapter import LLMResponse
from agora.fleet.orchestrator import Orchestrator
from agora.matrix.room_manager import RoomManager
from agora.observe.jsonl import (
    ArmSpec,
    ProfileSnapshot,
    RunObserver,
    RunRecord,
    TaskRecord,
)
from tests.conftest import FakeLLM, tool_call


def _profile() -> ProfileSnapshot:
    return ProfileSnapshot(
        name="p", model="ollama/qwen2.5:7b-instruct", num_ctx=8192,
        max_tokens=2048, temperature=0.0, seed=42, keep_alive="30m",
    )


def _observer(tmp_path: Path, **over) -> RunObserver:
    kwargs = dict(
        run_id="run-1",
        output_dir=tmp_path / "out",
        probe_name="probe",
        flow_path="flows/x.yaml",
        project_name="proj",
        profile=_profile(),
        arm=ArmSpec(),
        ollama_version="0.1.0",
        git_commit="abc1234",
        host="box",
    )
    kwargs.update(over)
    return RunObserver(**kwargs)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --------------------------------------------------------------- output dir resolution


def test_resolve_output_dir_precedence(monkeypatch) -> None:
    monkeypatch.delenv("AGORA_RUN_OUTPUT_DIR", raising=False)
    # default
    assert RunObserver.resolve_output_dir("xyz") == Path("runs_out") / "_default" / "xyz"
    # env
    monkeypatch.setenv("AGORA_RUN_OUTPUT_DIR", "some/dir")
    assert RunObserver.resolve_output_dir("xyz") == Path("some/dir")
    # explicit override beats env
    assert RunObserver.resolve_output_dir("xyz", "override/dir") == Path("override/dir")


# --------------------------------------------------------------- fake-orchestrator drive


def test_observer_drive_emits_valid_jsonl(tmp_path: Path) -> None:
    """Drive N task-completion events + a run event; re-parse via the schema."""
    obs = _observer(tmp_path)
    n = 5
    for i in range(n):
        obs.record_task(
            task_id=f"t{i}",
            task_index=i,
            role="implementer",
            task_kind="code_body",
            status="passed" if i % 2 == 0 else "failed",
            first_pass=i % 2 == 0,
            loopback_count=0 if i % 2 == 0 else 1,
            iterations=i + 1,
            postconditions=[{"name": "ok", "passed": True}],
            tool_calls_total=i,
            tools_used=["write_file", "mark_complete"],
            failure_category=None if i % 2 == 0 else "postcondition",
            failure_detail=None if i % 2 == 0 else "ok",
        )
    obs.record_run(
        duration_s=12.5,
        success=True,
        exit_code=0,
        tasks_total=n,
        tasks_passed=3,
        tasks_failed=2,
        tasks_first_pass=3,
        async_leak_hits=0,
        model_offloaded=None,
        tokens_in=100,
        tokens_out=200,
    )
    obs.close()

    tasks = _read_jsonl(tmp_path / "out" / "tasks.jsonl")
    runs = _read_jsonl(tmp_path / "out" / "run.jsonl")
    assert len(tasks) == n
    assert len(runs) == 1
    # Every line round-trips through the pinned schema.
    parsed_tasks = [TaskRecord.model_validate(t) for t in tasks]
    run = RunRecord.model_validate(runs[0])
    assert all(t.schema_version == 1 for t in parsed_tasks)
    assert run.schema_version == 1
    assert run.run_id == "run-1"
    assert all(t.run_id == "run-1" for t in parsed_tasks)
    assert run.tasks_passed == 3
    # Profile is a full snapshot.
    assert run.profile.seed == 42
    assert run.profile.num_ctx == 8192


def test_async_leak_scan_from_log(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    log.write_text(
        "ok line\n"
        "RuntimeWarning: coroutine 'x' was never awaited\n"
        "Unclosed client session\n"
        "another ok\n",
        encoding="utf-8",
    )
    obs = _observer(tmp_path, log_path=log)
    obs.record_run(
        duration_s=1.0, success=False, exit_code=1, tasks_total=0,
        tasks_passed=0, tasks_failed=0, tasks_first_pass=0,
        model_offloaded=None, tokens_in=0, tokens_out=0,
    )
    obs.close()
    run = RunRecord.model_validate(_read_jsonl(tmp_path / "out" / "run.jsonl")[0])
    assert run.async_leak_hits == 2


# --------------------------------------------------------------- derive-from-result


def _fake_task(tid, output_path="", pc_names=(), stage_kinds=(), agent_id="impl"):
    spec = SimpleNamespace(postconditions=[SimpleNamespace(name=n) for n in pc_names])
    stages = [SimpleNamespace(kind=k) for k in stage_kinds]
    return SimpleNamespace(
        id=tid, output_path=output_path, spec=spec, stages=stages, agent_id=agent_id
    )


def _fake_result(**over):
    base = dict(
        success=True,
        output="done",
        postcondition_results=[("ok", True, "")],
        iterations=2,
        tool_calls_total=4,
        tool_calls_structured=3,
        tool_calls_text_fallback=1,
        tool_calls_malformed=0,
        tool_call_unknown_name=0,
        tools_used=["mark_complete", "write_file"],
        first_text_fallback_iteration=1,
        duration_s=3.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_record_task_from_result_first_pass_and_tool_stats(tmp_path: Path) -> None:
    obs = _observer(tmp_path)
    task = _fake_task("write_tests", output_path="test_bot.py")
    obs.task_started("write_tests")
    rec = obs.record_task_from_result(
        task=task, result=_fake_result(), role="tester", task_index=0
    )
    assert rec.task_kind == "test_authoring"
    assert rec.status == "passed"
    assert rec.first_pass is True
    assert rec.loopback_count == 0
    assert rec.tool_calls_text_fallback == 1
    assert rec.first_text_fallback_iteration == 1
    assert rec.tools_used == ["mark_complete", "write_file"]


def test_record_task_from_result_loopback_and_failure(tmp_path: Path) -> None:
    obs = _observer(tmp_path)
    task = _fake_task("build", output_path="bot.py")
    # Two executions ⇒ one loop-back; failure with a failed postcondition.
    obs.task_started("build")
    obs.task_started("build")
    rec = obs.record_task_from_result(
        task=task,
        result=_fake_result(
            success=False,
            postcondition_results=[("bot_py_py_compiles", False, "SyntaxError")],
        ),
        role="implementer",
        task_index=1,
    )
    assert rec.status == "failed"
    assert rec.first_pass is False
    assert rec.loopback_count == 1
    assert rec.failure_category == "postcondition"
    assert rec.failure_detail == "bot_py_py_compiles"


def test_record_task_from_result_skipped(tmp_path: Path) -> None:
    obs = _observer(tmp_path)
    task = _fake_task("never_ran", output_path="x.py")
    rec = obs.record_task_from_result(
        task=task, result=None, role="implementer", task_index=2
    )
    assert rec.status == "skipped"
    assert rec.first_pass is False


# --------------------------------------------------------------- real orchestrator


def _passing_spec() -> Specification:
    return Specification(
        postconditions=(make_predicate("ok", "true", lambda _ctx: (True, "")),),
        description="trivial",
    )


def _simple_llm() -> FakeLLM:
    return FakeLLM(
        [
            LLMResponse(content="", tool_calls=(tool_call("mark_complete", {"summary": "x"}),)),
            LLMResponse(content="done"),
            LLMResponse(content="[]"),
        ]
        * 20
    )


async def test_observer_integration_via_orchestrator(tmp_path, fake_matrix_client) -> None:
    """End-to-end: a real run_project with the observer wired emits valid files."""
    obs = _observer(tmp_path)
    mgr = RoomManager(fake_matrix_client, homeserver_name="agora.local")
    orch = Orchestrator(
        matrix_client=fake_matrix_client,
        room_manager=mgr,
        llm_factory=lambda _m: _simple_llm(),
        work_dir=str(tmp_path / "work"),
        observer=obs,
    )
    agents = [AgentConfig(name="impl", role=AgentRole.IMPLEMENTER)]
    tasks = [
        Task(id=f"t{i}", spec=_passing_spec(), description=f"task {i}",
             agent_id="impl", status=TaskStatus.PENDING)
        for i in range(3)
    ]
    result = await orch.run_project("demo", agents, tasks)
    assert result.success is True

    out = tmp_path / "out"
    assert (out / "run.jsonl").is_file()
    assert (out / "tasks.jsonl").is_file()
    task_lines = _read_jsonl(out / "tasks.jsonl")
    run_lines = _read_jsonl(out / "run.jsonl")
    parsed = [TaskRecord.model_validate(t) for t in task_lines]
    run = RunRecord.model_validate(run_lines[0])
    assert len(parsed) == 3
    assert run.tasks_total == 3
    assert run.tasks_passed == 3
    assert run.tasks_first_pass == 3
    assert run.success is True
    # Roles propagated from the agent config.
    assert all(p.role == "implementer" for p in parsed)
    # mark_complete was the model's tool call → counted, no text fallback.
    assert all(p.tool_calls_total >= 1 for p in parsed)
    assert all(p.first_text_fallback_iteration is None for p in parsed)


async def test_orchestrator_without_observer_is_backcompat(tmp_path, fake_matrix_client) -> None:
    """No observer ⇒ no files emitted, run still succeeds (back-compat)."""
    mgr = RoomManager(fake_matrix_client, homeserver_name="agora.local")
    orch = Orchestrator(
        matrix_client=fake_matrix_client,
        room_manager=mgr,
        llm_factory=lambda _m: _simple_llm(),
        work_dir=str(tmp_path / "work"),
    )
    agents = [AgentConfig(name="impl", role=AgentRole.IMPLEMENTER)]
    tasks = [Task(id="t0", spec=_passing_spec(), description="x",
                  agent_id="impl", status=TaskStatus.PENDING)]
    result = await orch.run_project("demo2", agents, tasks)
    assert result.success is True
    # No run.jsonl written anywhere under tmp_path.
    assert not list(tmp_path.glob("**/run.jsonl"))


@pytest.mark.skipif(
    os.getenv("AGORA_E2E") != "1", reason="AGORA_E2E=1 gates the live smoke test"
)
async def test_observer_smoke_minimal_flow(tmp_path, fake_matrix_client) -> None:
    """Smoke: a minimal flow runs with the observer enabled; files validate."""
    obs = _observer(tmp_path)
    mgr = RoomManager(fake_matrix_client, homeserver_name="agora.local")
    orch = Orchestrator(
        matrix_client=fake_matrix_client,
        room_manager=mgr,
        llm_factory=lambda _m: _simple_llm(),
        work_dir=str(tmp_path / "work"),
        observer=obs,
    )
    agents = [AgentConfig(name="impl", role=AgentRole.IMPLEMENTER)]
    tasks = [Task(id="only", spec=_passing_spec(), description="x",
                  agent_id="impl", status=TaskStatus.PENDING)]
    await orch.run_project("smoke", agents, tasks)
    assert RunRecord.model_validate(_read_jsonl(tmp_path / "out" / "run.jsonl")[0])
    assert [TaskRecord.model_validate(t) for t in _read_jsonl(tmp_path / "out" / "tasks.jsonl")]
