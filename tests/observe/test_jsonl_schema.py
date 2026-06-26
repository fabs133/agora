"""Unit tests for the JSONL schema v1 models + the task_kind classifier."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agora.observe.jsonl import (
    SCHEMA_VERSION,
    ArmSpec,
    ProfileSnapshot,
    RunRecord,
    TaskRecord,
    classify_task_kind,
    derive_failure,
    derive_status,
)


def _profile() -> ProfileSnapshot:
    return ProfileSnapshot(
        name="qwen-coder-7b",
        model="ollama/qwen2.5-coder:7b",
        num_ctx=8192,
        max_tokens=2048,
        temperature=0.0,
        seed=42,
        keep_alive="30m",
    )


def _task_kwargs(**over):
    base = dict(
        run_id="r1",
        task_id="t1",
        task_index=0,
        role="implementer",
        task_kind="code_body",
        status="passed",
        first_pass=True,
        loopback_count=0,
        iterations=3,
    )
    base.update(over)
    return base


def _run_kwargs(**over):
    base = dict(
        run_id="r1",
        started_at="2026-06-26T00:00:00+00:00",
        ended_at="2026-06-26T00:01:00+00:00",
        duration_s=60.0,
        probe_name="probe",
        flow_path="flows/x.yaml",
        project_name="proj",
        profile=_profile(),
        arm=ArmSpec(),
        success=True,
        exit_code=0,
        tasks_total=1,
        tasks_passed=1,
        tasks_failed=0,
        tasks_first_pass=1,
        async_leak_hits=0,
        tokens_in=10,
        tokens_out=20,
        ollama_version="0.1.0",
        git_commit="abc1234",
        host="box",
    )
    base.update(over)
    return base


# --------------------------------------------------------------- schema_version


def test_schema_version_defaults_to_one() -> None:
    assert SCHEMA_VERSION == 1
    assert TaskRecord(**_task_kwargs()).schema_version == 1
    assert RunRecord(**_run_kwargs()).schema_version == 1


def test_schema_version_is_pinned() -> None:
    with pytest.raises(ValidationError):
        TaskRecord(**_task_kwargs(schema_version=2))
    with pytest.raises(ValidationError):
        RunRecord(**_run_kwargs(schema_version=2))


# --------------------------------------------------------------- required fields


def test_task_record_requires_core_fields() -> None:
    with pytest.raises(ValidationError):
        TaskRecord(run_id="r1")  # missing task_id, task_index, role, ...


def test_run_record_requires_profile_snapshot() -> None:
    kwargs = _run_kwargs()
    del kwargs["profile"]
    with pytest.raises(ValidationError):
        RunRecord(**kwargs)


def test_profile_snapshot_carries_full_config() -> None:
    p = _profile()
    # Full snapshot, not just the name.
    assert p.model == "ollama/qwen2.5-coder:7b"
    assert p.num_ctx == 8192
    assert p.seed == 42
    # extra fields forbidden.
    with pytest.raises(ValidationError):
        ProfileSnapshot(model="m", bogus=1)


# --------------------------------------------------------------- closed vocab


def test_task_kind_closed_vocab() -> None:
    with pytest.raises(ValidationError):
        TaskRecord(**_task_kwargs(task_kind="design"))


def test_status_closed_vocab() -> None:
    with pytest.raises(ValidationError):
        TaskRecord(**_task_kwargs(status="done"))


def test_failure_category_closed_vocab_and_nullable() -> None:
    # None is allowed.
    rec = TaskRecord(**_task_kwargs(failure_category=None))
    assert rec.failure_category is None
    # A non-vocab value is rejected.
    with pytest.raises(ValidationError):
        TaskRecord(**_task_kwargs(failure_category="boom"))
    # A vocab value is accepted.
    assert (
        TaskRecord(**_task_kwargs(status="failed", first_pass=False, failure_category="postcondition")).failure_category
        == "postcondition"
    )


def test_arm_closed_vocab() -> None:
    assert ArmSpec(scaffolding="lean", strictness="permissive").scaffolding == "lean"
    with pytest.raises(ValidationError):
        ArmSpec(scaffolding="medium")
    with pytest.raises(ValidationError):
        ArmSpec(strictness="loose")


# --------------------------------------------------------------- task_kind classifier
# One fixture per vocabulary value.


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (dict(output_path="kb/intro.md"), "research"),
        (dict(output_path="plan/api_spec.md"), "api_spec"),
        (dict(output_path="bot.py"), "code_body"),
        (dict(output_path="test_bot.py"), "test_authoring"),
        (dict(output_path="tests/test_x.py"), "test_authoring"),
        (
            dict(output_path="", postcondition_names=["test_bot_py", "pytest_passes"]),
            "test_run",
        ),
        (dict(output_path="", role="reviewer"), "review"),
        (
            dict(output_path="plan/plan.yaml", stage_kinds=["framework_finalize_plan"]),
            "framework_step",
        ),
    ],
)
def test_classify_task_kind_covers_every_vocab_value(kwargs, expected) -> None:
    assert classify_task_kind(**kwargs) == expected


def test_classify_task_kind_unclassifiable_warns_and_defaults(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        assert classify_task_kind() == "code_body"
    assert any("unclassifiable" in r.message for r in caplog.records)


def test_classify_api_spec_via_postcondition() -> None:
    assert (
        classify_task_kind(
            output_path="", postcondition_names=["plan_api_spec_md_is_valid"]
        )
        == "api_spec"
    )


# --------------------------------------------------------------- derivation helpers


def test_derive_status_paths() -> None:
    assert derive_status(True, "anything") == "passed"
    assert derive_status(False, "ERROR: RuntimeError: boom") == "error"
    assert derive_status(False, "postcondition X failed") == "failed"


def test_derive_failure_postcondition() -> None:
    cat, detail = derive_failure(
        status="failed",
        output="",
        postcondition_results=[("file_exists_bot", False, "missing"), ("ok", True, "")],
    )
    assert cat == "postcondition"
    assert detail == "file_exists_bot"


def test_derive_failure_model_error() -> None:
    cat, detail = derive_failure(
        status="error", output="ERROR: ValueError: nope", postcondition_results=[]
    )
    assert cat == "model_error"


def test_derive_failure_tool_error_when_no_failed_pc() -> None:
    cat, detail = derive_failure(
        status="failed",
        output="",
        postcondition_results=[],
        tool_call_unknown_name=2,
    )
    assert cat == "tool_error"


def test_derive_failure_none_for_passed() -> None:
    assert derive_failure(status="passed", output="", postcondition_results=[]) == (
        None,
        None,
    )
