from pathlib import Path

import pytest

from agora.core.agent import AgentConfig
from agora.core.errors import AgoraError
from agora.core.flow import Flow, TaskTemplate, instantiate_flow, load_flow, save_flow
from agora.core.types import AgentRole, TaskStatus

VALID_YAML = """
name: demo
description: a demo flow
agents:
  - name: architect
    role: architect
    instructions: design things
  - name: impl
    role: implementer
task_graph:
  - id: t1
    assigned_to: architect
    description: plan
    postcondition_descriptions: [produces plan]
  - id: t2
    assigned_to: impl
    description: build it
    depends_on: [t1]
"""


def test_load_flow_valid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "flow.yaml"
    p.write_text(VALID_YAML, encoding="utf-8")
    flow = load_flow(p)
    assert flow.name == "demo"
    assert len(flow.agents) == 2
    assert {a.name for a in flow.agents} == {"architect", "impl"}
    assert flow.task_graph[1].depends_on == ("t1",)


def test_load_flow_invalid_schema(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("name: only-a-name\n", encoding="utf-8")
    with pytest.raises(AgoraError):
        load_flow(p)


def test_load_flow_unknown_agent_ref(tmp_path: Path) -> None:
    bad_yaml = """
name: bad
agents:
  - name: a
    role: architect
task_graph:
  - id: t1
    assigned_to: nonexistent
    description: x
"""
    p = tmp_path / "bad2.yaml"
    p.write_text(bad_yaml, encoding="utf-8")
    with pytest.raises(AgoraError, match="assigned_to"):
        load_flow(p)


def test_instantiate_flow_generates_uuids(tmp_path: Path) -> None:
    p = tmp_path / "flow.yaml"
    p.write_text(VALID_YAML, encoding="utf-8")
    flow = load_flow(p)
    _, tasks = instantiate_flow(flow, "proj")
    assert len(tasks) == 2
    ids = {t.id for t in tasks}
    # Fresh UUIDs — not the template ids.
    assert "t1" not in ids and "t2" not in ids
    assert all(t.status == TaskStatus.PENDING for t in tasks)


def test_instantiate_flow_resolves_agent_refs(tmp_path: Path) -> None:
    p = tmp_path / "flow.yaml"
    p.write_text(VALID_YAML, encoding="utf-8")
    flow = load_flow(p)
    _, tasks = instantiate_flow(flow, "proj")
    by_desc = {t.description: t for t in tasks}
    t1 = by_desc["plan"]
    t2 = by_desc["build it"]
    assert t1.agent_id == "architect"
    assert t2.agent_id == "impl"
    # t2's depends_on should reference t1's new UUID
    assert t2.depends_on == (t1.id,)


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    flow = Flow(
        name="rt",
        description="round trip",
        agents=(
            AgentConfig(name="a", role=AgentRole.ARCHITECT, instructions="do it"),
        ),
        task_graph=(
            TaskTemplate(
                id="t1",
                assigned_to="a",
                description="task one",
                postcondition_descriptions=("has output",),
            ),
        ),
    )
    p = tmp_path / "rt.yaml"
    save_flow(flow, p)
    loaded = load_flow(p)
    assert loaded == flow


# --------------------- Sprint 6: versioning, parameters, includes


def test_load_flow_rejects_unsupported_version(tmp_path: Path) -> None:
    p = tmp_path / "v.yaml"
    p.write_text(
        'version: "9.9"\nname: x\nagents:\n  - {name: a, role: architect}\n',
        encoding="utf-8",
    )
    with pytest.raises(AgoraError, match="unsupported flow version"):
        load_flow(p)


def test_instantiate_flow_substitutes_project_name(tmp_path: Path) -> None:
    p = tmp_path / "sub.yaml"
    p.write_text(
        'version: "1.0"\n'
        "name: sub\n"
        "agents:\n"
        "  - name: impl\n"
        "    role: implementer\n"
        "    instructions: 'work on ${project_name}'\n"
        "task_graph:\n"
        "  - id: t1\n"
        "    assigned_to: impl\n"
        "    description: 'Build ${project_name}'\n",
        encoding="utf-8",
    )
    flow = load_flow(p)
    agents, tasks = instantiate_flow(flow, "my-app")
    assert "work on my-app" in agents[0].instructions
    assert "Build my-app" in tasks[0].description


def test_instantiate_flow_user_variables(tmp_path: Path) -> None:
    p = tmp_path / "v.yaml"
    p.write_text(
        'version: "1.0"\n'
        "name: v\n"
        "agents:\n"
        "  - name: impl\n"
        "    role: implementer\n"
        "task_graph:\n"
        "  - id: t1\n"
        "    assigned_to: impl\n"
        "    description: 'Write to ${repo_path}'\n",
        encoding="utf-8",
    )
    flow = load_flow(p)
    _, tasks = instantiate_flow(flow, "p", variables={"repo_path": "/tmp/repo"})
    assert "Write to /tmp/repo" in tasks[0].description


def test_instantiate_flow_unknown_variable_raises(tmp_path: Path) -> None:
    p = tmp_path / "u.yaml"
    p.write_text(
        'version: "1.0"\n'
        "name: u\n"
        "agents:\n"
        "  - name: impl\n"
        "    role: implementer\n"
        "task_graph:\n"
        "  - id: t1\n"
        "    assigned_to: impl\n"
        "    description: 'broken ${unknown_var}'\n",
        encoding="utf-8",
    )
    flow = load_flow(p)
    with pytest.raises(AgoraError, match="unknown variable"):
        instantiate_flow(flow, "x")


def test_flow_includes_merges_child(tmp_path: Path) -> None:
    child = tmp_path / "child.yaml"
    child.write_text(
        'version: "1.0"\n'
        "name: child\n"
        "agents:\n"
        "  - name: architect\n"
        "    role: architect\n"
        "task_graph:\n"
        "  - id: plan\n"
        "    assigned_to: architect\n"
        "    description: plan\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.yaml"
    parent.write_text(
        'version: "1.0"\n'
        "name: parent\n"
        "includes: [child.yaml]\n"
        "agents:\n"
        "  - name: impl\n"
        "    role: implementer\n"
        "task_graph:\n"
        "  - id: build\n"
        "    assigned_to: impl\n"
        "    description: build\n",
        encoding="utf-8",
    )
    flow = load_flow(parent)
    # Both agents merged.
    names = {a.name for a in flow.agents}
    assert names == {"architect", "impl"}
    # Child task id namespaced by child flow name.
    task_ids = {t.id for t in flow.task_graph}
    assert "child:plan" in task_ids
    assert "build" in task_ids


def test_flow_includes_cycle_detected(tmp_path: Path) -> None:
    a_yaml = tmp_path / "a.yaml"
    b_yaml = tmp_path / "b.yaml"
    a_yaml.write_text(
        'version: "1.0"\n'
        "name: a\n"
        "includes: [b.yaml]\n"
        "agents: [{name: aa, role: architect}]\n",
        encoding="utf-8",
    )
    b_yaml.write_text(
        'version: "1.0"\n'
        "name: b\n"
        "includes: [a.yaml]\n"
        "agents: [{name: bb, role: implementer}]\n",
        encoding="utf-8",
    )
    with pytest.raises(AgoraError, match="cycle"):
        load_flow(a_yaml)


def test_flow_empty_agents_and_no_includes_is_invalid(tmp_path: Path) -> None:
    p = tmp_path / "e.yaml"
    p.write_text('version: "1.0"\nname: e\n', encoding="utf-8")
    with pytest.raises(AgoraError, match="at least one agent or include"):
        load_flow(p)


def test_builtin_solo_flow_loads() -> None:
    """The ``flows/builtin/solo.yaml`` shipped with the package is valid."""
    builtin = (
        Path(__file__).resolve().parents[2] / "flows" / "builtin" / "solo.yaml"
    )
    if not builtin.is_file():  # pragma: no cover — only in dev checkouts
        pytest.skip("builtin flow not present")
    flow = load_flow(builtin)
    assert flow.name == "solo"


def test_builtin_architect_implementer_tester_flow_loads() -> None:
    builtin = (
        Path(__file__).resolve().parents[2]
        / "flows"
        / "builtin"
        / "architect-implementer-tester.yaml"
    )
    if not builtin.is_file():  # pragma: no cover
        pytest.skip("builtin flow not present")
    flow = load_flow(builtin)
    assert {a.name for a in flow.agents} == {"architect", "impl", "tester"}
    task_ids = {t.id for t in flow.task_graph}
    assert {"design", "implement", "test"} <= task_ids
