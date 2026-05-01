"""Round-trip gate: ``flows/fastapi-crud.plan.yaml`` must produce a DAG structurally
equivalent to what :func:`scripts.run_fastapi_crud_test.build_tasks` produces.

This test does not hit Ollama. It compares the in-memory ``(agents, tasks,
staged_tasks)`` triples by task id, dependency graph, Specification fingerprint
(which captures postcondition names + descriptions), stage counts, and output
paths. If this passes, running the live plan runner against the YAML should
produce the same behaviour as the hand-written Python runner.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agora.plan.loader import instantiate_plan, load_plan

REPO_ROOT = Path(__file__).resolve().parents[2]
FASTAPI_RUNNER = REPO_ROOT / "scripts" / "run_fastapi_crud_test.py"
FASTAPI_PLAN = REPO_ROOT / "flows" / "fastapi-crud.plan.yaml"
MODEL = "ollama/qwen2.5:7b-instruct"


@pytest.fixture(scope="module")
def python_built():
    """Load the runner module and build (agents, tasks, staged_tasks) via the Python path."""
    if not FASTAPI_RUNNER.is_file():
        pytest.skip(f"{FASTAPI_RUNNER} missing")
    spec = importlib.util.spec_from_file_location("_fastapi_runner", FASTAPI_RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    agents = mod.build_agents()
    tasks = mod.build_tasks()
    staged = mod.build_staged_tasks(tasks)
    return agents, tasks, staged


@pytest.fixture(scope="module")
def yaml_built():
    """Load the plan YAML and instantiate the same triple."""
    if not FASTAPI_PLAN.is_file():
        pytest.skip(f"{FASTAPI_PLAN} missing")
    plan = load_plan(FASTAPI_PLAN)
    return instantiate_plan(
        plan,
        project_name="fastapi-crud",
        variables={"model": MODEL},
    )


def test_task_counts_match(python_built, yaml_built):
    _, py_tasks, py_staged = python_built
    _, yaml_tasks, yaml_staged = yaml_built
    assert len(py_tasks) == len(yaml_tasks) == 13
    # The fastapi-crud runner stages 7 tasks (all build_* + write_requirements + write_tests).
    assert len(py_staged) == len(yaml_staged) == 7


def test_task_ids_match(python_built, yaml_built):
    _, py_tasks, _ = python_built
    _, yaml_tasks, _ = yaml_built
    assert [t.id for t in py_tasks] == [t.id for t in yaml_tasks]


def test_task_depends_on_match(python_built, yaml_built):
    _, py_tasks, _ = python_built
    _, yaml_tasks, _ = yaml_built
    for py_t, yaml_t in zip(py_tasks, yaml_tasks, strict=True):
        assert py_t.depends_on == yaml_t.depends_on, (
            f"depends_on mismatch for {py_t.id}: {py_t.depends_on} vs {yaml_t.depends_on}"
        )


def test_task_output_paths_match(python_built, yaml_built):
    _, py_tasks, _ = python_built
    _, yaml_tasks, _ = yaml_built
    for py_t, yaml_t in zip(py_tasks, yaml_tasks, strict=True):
        assert py_t.output_path == yaml_t.output_path, (
            f"output_path mismatch for {py_t.id}"
        )


def test_task_agent_assignments_match(python_built, yaml_built):
    _, py_tasks, _ = python_built
    _, yaml_tasks, _ = yaml_built
    for py_t, yaml_t in zip(py_tasks, yaml_tasks, strict=True):
        assert py_t.agent_id == yaml_t.agent_id


def test_postcondition_names_match(python_built, yaml_built):
    """Every task's postcondition names match byte-for-byte (the fingerprint contract)."""
    _, py_tasks, _ = python_built
    _, yaml_tasks, _ = yaml_built
    for py_t, yaml_t in zip(py_tasks, yaml_tasks, strict=True):
        py_names = [p.name for p in py_t.spec.postconditions]
        yaml_names = [p.name for p in yaml_t.spec.postconditions]
        assert py_names == yaml_names, f"postcondition mismatch for {py_t.id}"


def test_postcondition_descriptions_match(python_built, yaml_built):
    """Postcondition description (what make_predicate set) matches. Combined with
    the name match this is equivalent to a predicate-only fingerprint check —
    stronger than fingerprint alone because it catches name-collisions with
    different descriptions, which fingerprint would hide."""
    _, py_tasks, _ = python_built
    _, yaml_tasks, _ = yaml_built
    for py_t, yaml_t in zip(py_tasks, yaml_tasks, strict=True):
        py_descs = [p.description for p in py_t.spec.postconditions]
        yaml_descs = [p.description for p in yaml_t.spec.postconditions]
        assert py_descs == yaml_descs, (
            f"postcondition description mismatch for {py_t.id}"
        )


def test_staged_task_keys_match(python_built, yaml_built):
    _, _, py_staged = python_built
    _, _, yaml_staged = yaml_built
    assert sorted(py_staged) == sorted(yaml_staged)


def test_staged_stage_count_and_shape_match(python_built, yaml_built):
    _, _, py_staged = python_built
    _, _, yaml_staged = yaml_built
    for tid in py_staged:
        py_stages = py_staged[tid].stages
        yaml_stages = yaml_staged[tid].stages
        assert len(py_stages) == len(yaml_stages), f"stage count mismatch for {tid}"
        for py_s, yaml_s in zip(py_stages, yaml_stages, strict=True):
            assert py_s.name == yaml_s.name
            assert py_s.instruction == yaml_s.instruction, (
                f"stage instruction mismatch for {tid}:{py_s.name}"
            )
            assert tuple(py_s.context_files) == tuple(yaml_s.context_files)
            assert py_s.max_iterations == yaml_s.max_iterations


def test_agents_match(python_built, yaml_built):
    py_agents, _, _ = python_built
    yaml_agents, _, _ = yaml_built
    assert len(py_agents) == len(yaml_agents)
    for py_a, yaml_a in zip(py_agents, yaml_agents, strict=True):
        assert py_a.name == yaml_a.name
        assert py_a.role == yaml_a.role
        assert py_a.model == yaml_a.model, (
            f"model mismatch for {py_a.name}: {py_a.model!r} vs {yaml_a.model!r}"
        )
        # Instructions may differ in trailing whitespace — normalize.
        assert py_a.instructions.strip() == yaml_a.instructions.strip()
