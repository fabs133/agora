"""Stage-level tests for the v2.4 test-authoring framework stages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.contract import Specification, make_predicate
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus
from agora.fleet.agent_runtime import AgentRuntime
from agora.fleet.control import OrchestratorControl
from agora.fleet.inner_tools import ToolContext
from agora.fleet.stage_runner import Stage, StagedTask, StageRunner


class _FakeMatrixClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def send_event(self, room_id: str, event_type: str, content: dict) -> str:
        self.sent.append((room_id, event_type, content))
        return f"$evt_{len(self.sent)}"


class _FakeLLM:
    async def complete(self, *args, **kwargs):
        raise AssertionError("framework stage should not invoke the LLM")


def _make_runtime(tmp_path: Path):
    client = _FakeMatrixClient()
    control = OrchestratorControl(
        project_room_id="!room:x", matrix_client=client  # type: ignore[arg-type]
    )
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=client,  # type: ignore[arg-type]
        agent_room_id="!agent:x",
        project_room_id="!room:x",
        control=control,
    )
    runtime = AgentRuntime(
        llm=_FakeLLM(),  # type: ignore[arg-type]
        matrix_client=client,  # type: ignore[arg-type]
        tool_context=ctx,
    )
    return runtime, ctx


@pytest.fixture
def _identity() -> AgentIdentity:
    return AgentIdentity(
        agent_id="tester",
        room_id="!agent:x",
        config=AgentConfig(name="tester", role=AgentRole.TESTER),
    )


def _single_stage_task(stage: Stage) -> StagedTask:
    task = Task(
        id="t",
        spec=Specification(
            postconditions=(
                make_predicate("_trivial", "pass", lambda _c: (True, "")),
            ),
            description="framework stage",
        ),
        description="framework stage",
        agent_id="tester",
        status=TaskStatus.PENDING,
    )
    return StagedTask(task=task, stages=[stage])


def _setup_project_skeleton(tmp_path: Path) -> None:
    """Create a minimal brief + src/ layout so the scaffold has something to work with."""
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "brief.md").write_text(
        "# Brief: URL shortener\n\n"
        "## Key deliverables\n"
        "- Add a URL and get a hash back\n"
        "- Look up the original URL\n",
        encoding="utf-8",
    )
    pkg = tmp_path / "src" / "url_shortener_mvp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "domain.py").write_text(
        "class Shortener:\n    def shorten(self, url): return 'abc123'\n",
        encoding="utf-8",
    )


# ========================================================= plan_scaffold_tests


async def test_scaffold_tests_writes_test_file_with_real_imports(
    tmp_path: Path, _identity
):
    runtime, ctx = _make_runtime(tmp_path)
    _setup_project_skeleton(tmp_path)

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="scaffold",
            kind="plan_scaffold_tests",
            output_path="tests/test_main.py",
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True, outcome.postcondition_results

    out = tmp_path / "tests" / "test_main.py"
    assert out.is_file()
    content = out.read_text(encoding="utf-8")
    assert "import pytest" in content
    assert "from url_shortener_mvp.domain import Shortener" in content
    assert content.count("pytest.skip(") == 2  # two deliverables → two stubs
    assert "tests/test_main.py" in ctx.written_files


async def test_scaffold_tests_fails_without_output_path(tmp_path: Path, _identity):
    runtime, _ctx = _make_runtime(tmp_path)
    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(name="scaffold", kind="plan_scaffold_tests", output_path="")
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    assert any(
        "output_path is required" in r[2] for r in outcome.postcondition_results
    )


async def test_scaffold_tests_uses_fallback_brief_from_validation_args(
    tmp_path: Path, _identity
):
    """When plan/brief.md is absent but the loader piped a fallback brief
    through validation_args, the scaffolder parses that instead of
    emitting the single-generic-stub fallback."""
    runtime, _ctx = _make_runtime(tmp_path)
    # No plan/brief.md. src has one module.
    pkg = tmp_path / "src" / "app"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("def run(): pass\n", encoding="utf-8")

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="scaffold",
            kind="plan_scaffold_tests",
            output_path="tests/test_x.py",
            validation_args={
                "fallback_brief": "## Key deliverables\n- add a URL\n- list all URLs\n"
            },
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True

    body = (tmp_path / "tests" / "test_x.py").read_text(encoding="utf-8")
    assert body.count("pytest.skip(") == 2
    assert "test_add_a_url" in body
    assert "test_list_all_urls" in body
    # No single-generic-fallback line.
    assert "test_main_flow" not in body


async def test_scaffold_tests_handles_missing_brief(tmp_path: Path, _identity):
    """No brief → fallback to a single generic test stub (not a failure)."""
    runtime, _ctx = _make_runtime(tmp_path)
    # No plan/brief.md — framework tolerates.
    pkg = tmp_path / "src" / "app"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("def run(): pass\n", encoding="utf-8")

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(name="scaffold", kind="plan_scaffold_tests", output_path="tests/t.py")
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True

    body = (tmp_path / "tests" / "t.py").read_text(encoding="utf-8")
    assert "def test_main_flow" in body
    assert "from app.core import run" in body


async def test_scaffold_tests_idempotent_overwrite(tmp_path: Path, _identity):
    """Scaffold bypasses the write_file overwrite guard — re-running on
    a retry replaces the prior scaffold cleanly."""
    runtime, _ctx = _make_runtime(tmp_path)
    _setup_project_skeleton(tmp_path)

    # Pre-populate the target with bogus content from a simulated prior attempt.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text(
        "# stale content", encoding="utf-8"
    )

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="scaffold",
            kind="plan_scaffold_tests",
            output_path="tests/test_main.py",
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True
    body = (tmp_path / "tests" / "test_main.py").read_text(encoding="utf-8")
    assert "stale content" not in body
    assert "import pytest" in body


# ============================================================= plan_run_pytest


async def test_run_pytest_captures_output_on_pass(tmp_path: Path, _identity):
    runtime, _ctx = _make_runtime(tmp_path)
    # Write a minimal passing test.
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="verify",
            kind="plan_run_pytest",
            output_path="plan/kb/pytest_output.md",
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    # plan_run_pytest NEVER fails the stage — observational only.
    assert outcome.success is True

    out = tmp_path / "plan" / "kb" / "pytest_output.md"
    assert out.is_file()
    body = out.read_text(encoding="utf-8")
    assert "pytest output" in body
    # Either the exit code or 'passed: True' should be present.
    assert "exit code" in body


async def test_run_pytest_still_succeeds_on_pytest_failure(
    tmp_path: Path, _identity
):
    """A failing pytest run still emits the file and returns success —
    the pytest_passes postcondition is what gates; this stage just records."""
    runtime, _ctx = _make_runtime(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_fail.py").write_text(
        "def test_fails():\n    assert 1 + 1 == 3\n", encoding="utf-8"
    )

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="verify",
            kind="plan_run_pytest",
            output_path="plan/kb/pytest_output.md",
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True

    out = tmp_path / "plan" / "kb" / "pytest_output.md"
    assert out.is_file()


async def test_run_pytest_uses_default_output_path(tmp_path: Path, _identity):
    """When ``output_path`` is empty, defaults to ``plan/kb/pytest_output.md``."""
    runtime, _ctx = _make_runtime(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(name="verify", kind="plan_run_pytest", output_path="")
    )
    # Schema rejects empty output_path for plan_run_pytest? It actually accepts
    # it (output_path optional) — verify default path is used.
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True
    assert (tmp_path / "plan" / "kb" / "pytest_output.md").is_file()


# ========================================================= plan_derive_test_intent


class _CannedLLM:
    """LLM stub that returns a canned response per call, recording what it saw."""

    def __init__(self, canned: str = "This test verifies the feature.") -> None:
        self.canned = canned
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages,
        system="",
        tools=None,
        model="",
        max_tokens=4096,
    ):
        from agora.fleet.llm_adapter import LLMResponse

        self.calls.append(
            {"messages": messages, "system": system, "model": model}
        )
        return LLMResponse(content=self.canned, tool_calls=(), stop_reason="end_turn")


def _make_runtime_with_llm(tmp_path: Path, llm):
    client = _FakeMatrixClient()
    control = OrchestratorControl(
        project_room_id="!room:x", matrix_client=client  # type: ignore[arg-type]
    )
    ctx = ToolContext(
        work_dir=str(tmp_path),
        matrix_client=client,  # type: ignore[arg-type]
        agent_room_id="!agent:x",
        project_room_id="!room:x",
        control=control,
    )
    runtime = AgentRuntime(
        llm=llm,  # type: ignore[arg-type]
        matrix_client=client,  # type: ignore[arg-type]
        tool_context=ctx,
    )
    return runtime, ctx


def _scaffold_file(tmp_path: Path, rel: str = "tests/test_contract.py") -> Path:
    """Write a realistic scaffolded file with pytest.skip stubs."""
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "import pytest\n\n\n"
        'def test_add():\n    """Add a URL and get a hash back"""\n'
        '    pytest.skip("TODO: add")\n\n'
        'def test_lookup():\n    """Look up the original URL"""\n'
        '    pytest.skip("TODO: lookup")\n',
        encoding="utf-8",
    )
    return p


async def test_derive_test_intent_writes_one_section_per_test(
    tmp_path: Path, _identity
):
    llm = _CannedLLM(canned="This test verifies the add-and-hash round trip.")
    runtime, ctx = _make_runtime_with_llm(tmp_path, llm)
    _setup_project_skeleton(tmp_path)
    _scaffold_file(tmp_path)

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="derive_intent",
            kind="plan_derive_test_intent",
            output_path="plan/kb/test_intent.md",
            validation_args={
                "scaffold_path": "tests/test_contract.py",
                "mode": "contract",
            },
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True, outcome.postcondition_results[0][2]
    # One LLM call per test function.
    assert len(llm.calls) == 2
    # Output file has a section per test.
    out = (tmp_path / "plan" / "kb" / "test_intent.md").read_text(encoding="utf-8")
    assert "## test_add" in out
    assert "## test_lookup" in out
    assert "add-and-hash round trip" in out


async def test_derive_test_intent_fails_without_scaffold_path(
    tmp_path: Path, _identity
):
    llm = _CannedLLM()
    runtime, _ctx = _make_runtime_with_llm(tmp_path, llm)
    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="derive_intent",
            kind="plan_derive_test_intent",
            output_path="plan/kb/intent.md",
            validation_args={},  # no scaffold_path
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    assert "scaffold_path" in (outcome.postcondition_results[0][2] or "")
    # LLM was never called.
    assert len(llm.calls) == 0


async def test_derive_test_intent_uses_fallback_brief(tmp_path: Path, _identity):
    """When plan/brief.md is missing, falls back to validation_args['fallback_brief']."""
    llm = _CannedLLM()
    runtime, _ctx = _make_runtime_with_llm(tmp_path, llm)
    # Note: no _setup_project_skeleton — brief.md doesn't exist.
    _scaffold_file(tmp_path)

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="derive_intent",
            kind="plan_derive_test_intent",
            output_path="plan/kb/intent.md",
            validation_args={
                "scaffold_path": "tests/test_contract.py",
                "fallback_brief": (
                    "# Fallback\n\n## Key deliverables\n- Add a URL\n"
                ),
                "mode": "contract",
            },
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True, outcome.postcondition_results[0][2]
    # LLM prompt included the fallback brief content.
    first_msg = llm.calls[0]["messages"][0]["content"]
    assert "Fallback" in first_msg or "Add a URL" in first_msg


async def test_derive_test_intent_fails_without_brief(tmp_path: Path, _identity):
    """Neither plan/brief.md nor fallback_brief → fail loudly."""
    llm = _CannedLLM()
    runtime, _ctx = _make_runtime_with_llm(tmp_path, llm)
    _scaffold_file(tmp_path)

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="derive_intent",
            kind="plan_derive_test_intent",
            output_path="plan/kb/intent.md",
            validation_args={"scaffold_path": "tests/test_contract.py"},
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    assert "brief" in (outcome.postcondition_results[0][2] or "").lower()


async def test_derive_test_intent_fails_if_scaffold_missing(
    tmp_path: Path, _identity
):
    llm = _CannedLLM()
    runtime, _ctx = _make_runtime_with_llm(tmp_path, llm)
    _setup_project_skeleton(tmp_path)
    # No scaffold file.

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="derive_intent",
            kind="plan_derive_test_intent",
            output_path="plan/kb/intent.md",
            validation_args={
                "scaffold_path": "tests/does_not_exist.py",
                "mode": "contract",
            },
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is False
    assert "not found" in (outcome.postcondition_results[0][2] or "")


async def test_derive_test_intent_prompt_differs_by_mode(
    tmp_path: Path, _identity
):
    """Contract mode warns 'implementation does not exist'; impl mode doesn't."""
    llm = _CannedLLM()
    runtime, _ctx = _make_runtime_with_llm(tmp_path, llm)
    _setup_project_skeleton(tmp_path)
    _scaffold_file(tmp_path)

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="derive_intent",
            kind="plan_derive_test_intent",
            output_path="plan/kb/intent.md",
            validation_args={
                "scaffold_path": "tests/test_contract.py",
                "mode": "impl",
            },
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True, outcome.postcondition_results[0][2]
    first_msg = llm.calls[0]["messages"][0]["content"]
    assert "implementation exists" in first_msg.lower() or "concrete input" in first_msg.lower()


# ============================================ fill_test_body binding lifecycle (7.3)


async def test_scaffold_stage_binds_active_test_file(tmp_path: Path, _identity):
    runtime, ctx = _make_runtime(tmp_path)
    _setup_project_skeleton(tmp_path)
    assert ctx.active_test_file == ""

    runner = StageRunner(runtime)
    staged = _single_stage_task(
        Stage(
            name="scaffold",
            kind="plan_scaffold_tests",
            output_path="tests/test_contract.py",
            validation_args={"mode": "contract"},
        )
    )
    outcome = await runner.execute_staged_task(staged, _identity)
    assert outcome.success is True
    # Scaffold bound the path so the next stage's manifest picks up fill_test_body.
    assert ctx.active_test_file == "tests/test_contract.py"


async def test_new_task_clears_active_test_file(tmp_path: Path, _identity):
    """Per-task isolation: prior task's binding doesn't leak into this one."""
    runtime, ctx = _make_runtime(tmp_path)
    ctx.active_test_file = "tests/stale_from_prior_task.py"
    _setup_project_skeleton(tmp_path)

    runner = StageRunner(runtime)
    # Any staged task that doesn't run plan_scaffold_tests.
    from agora.core.contract import Specification, make_predicate
    from agora.core.task import Task
    from agora.core.types import TaskStatus
    task = Task(
        id="t", spec=Specification(
            postconditions=(make_predicate("_t", "pass", lambda c: (True, "")),),
            description="",
        ),
        description="",
        agent_id="tester",
        status=TaskStatus.PENDING,
    )
    staged = StagedTask(
        task=task,
        stages=[
            Stage(
                name="verify", kind="plan_run_pytest",
                output_path="plan/kb/pytest_output.md",
            )
        ],
    )
    await runner.execute_staged_task(staged, _identity)
    # execute_staged_task's reset cleared the stale binding at the start.
    assert ctx.active_test_file == ""
