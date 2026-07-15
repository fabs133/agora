"""Probe flow loads/instantiates; registry resolves the new names; gated E2E smoke."""

from __future__ import annotations

import os

import pytest

from agora.core.flow import load_flow
from agora.plan.loader import instantiate_plan, load_plan
from agora.plan.predicate_registry import build_predicate, list_registered_predicates

FLOW = "flows/tool-call-fidelity.plan.yaml"


def test_probe_flow_loads_and_validates() -> None:
    flow = load_flow(FLOW)
    assert flow.name == "tool-call-fidelity"
    assert flow.probe_version == 7  # v7: bare (form-B) tool messages + LF seeds
    assert [a.name for a in flow.agents] == ["probe_impl"]
    assert flow.agents[0].model == ""  # profile-driven
    assert [t.id for t in flow.task_graph] == [
        "small_chain",
        "loop_depth",
        "content_robustness",
    ]


def test_probe_flow_instantiates_with_staged_iteration_caps() -> None:
    agents, tasks, staged = instantiate_plan(load_plan(FLOW), "tool-call-fidelity")
    assert {t.id for t in tasks} == {"small_chain", "loop_depth", "content_robustness"}
    # Each probe task is a single-stage staged task with the spec'd iteration cap.
    caps = {tid: [s.max_iterations for s in st.stages] for tid, st in staged.items()}
    assert caps == {"small_chain": [5], "loop_depth": [12], "content_robustness": [4]}


def test_probe_registry_resolves_new_predicate_names() -> None:
    names = set(list_registered_predicates())
    for n in ("file_content_equals_seed", "file_content_equals_concat", "mark_complete_called"):
        assert n in names
    # And they build without error with the probe's arg shapes.
    build_predicate("file_content_equals_seed", {"path": "a", "seed_path": "b"})
    build_predicate(
        "file_content_equals_concat",
        {"path": "a", "first_path": "b", "second_path": "c"},
    )
    build_predicate("mark_complete_called", {})


@pytest.mark.slow
@pytest.mark.skipif(os.getenv("AGORA_E2E") != "1", reason="AGORA_E2E=1 gates the live probe")
async def test_probe_end_to_end_emits_jsonl(tmp_path) -> None:
    """Run the probe against a small local model; assert JSONL emits + validates.

    Requires a live Conduit + Ollama with qwen2.5:7b-instruct present.
    """
    import uuid

    from agora.fleet.profiles import apply_env_overrides, load_profiles
    from agora.observe.jsonl import (
        RunObserver,
        RunRecord,
        TaskRecord,
        profile_snapshot_from,
    )
    from agora.plan.harness import (
        build_matrix_client,
        build_orchestrator,
    )
    from tests.conftest import make_harness_config

    os.environ.setdefault("AGORA_LLM_MODEL", "ollama/qwen2.5:7b-instruct")
    profile = apply_env_overrides(load_profiles().select(os.getenv("AGORA_PROFILE", "")))
    cfg = make_harness_config(work_dir=tmp_path / "work", auto_hooks_enabled=False)

    from scripts.run_tool_call_fidelity import seed_probe_files

    seed_probe_files(cfg.work_dir, "tool-call-fidelity")
    agents, tasks, staged = instantiate_plan(load_plan(FLOW), "tool-call-fidelity")

    run_id = uuid.uuid4().hex
    out = tmp_path / "out"
    observer = RunObserver(
        run_id=run_id, output_dir=out, probe_name="tool-call-fidelity",
        flow_path=FLOW, project_name="tool-call-fidelity",
        profile=profile_snapshot_from(profile),
    )
    client = await build_matrix_client(cfg)
    try:
        orch = build_orchestrator(cfg, client, profile.model, profile=profile, observer=observer)
        await orch.run_project(
            "tool-call-fidelity", agents, tasks, max_loopbacks=0, staged_tasks=staged
        )
    finally:
        await client.close()

    import json

    run_rows = [json.loads(line) for line in (out / "run.jsonl").read_text().splitlines() if line]
    task_rows = [json.loads(line) for line in (out / "tasks.jsonl").read_text().splitlines() if line]
    assert RunRecord.model_validate(run_rows[0])
    assert [TaskRecord.model_validate(t) for t in task_rows]
    assert len(task_rows) == 3
