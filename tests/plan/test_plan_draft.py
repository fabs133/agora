"""Unit coverage for :class:`agora.plan.builder.PlanDraft`.

The draft holds in-progress plan state as the LLM calls the plan-authoring
tools. Every mutation validates at the boundary — these tests nail down that
contract so tool executors can trust ``AgoraError`` messages round-trip back
to the model cleanly.
"""

from __future__ import annotations

import pytest

from agora.core.errors import AgoraError
from agora.core.flow import Flow, PostconditionRef, save_flow
from agora.plan.builder import PlanDraft
from agora.plan.loader import instantiate_plan, load_plan


@pytest.fixture
def draft() -> PlanDraft:
    d = PlanDraft()
    d.set_metadata("test-plan", "a test plan")
    d.set_agents(
        [
            {"name": "arch", "role": "architect", "instructions": "plan"},
            {"name": "impl", "role": "implementer", "instructions": "build"},
        ]
    )
    return d


# ---------------------------------------------------------------------- agents


def test_set_agents_requires_nonempty_list():
    d = PlanDraft()
    with pytest.raises(AgoraError, match="non-empty list"):
        d.set_agents([])


def test_set_agents_rejects_unknown_role():
    d = PlanDraft()
    with pytest.raises(AgoraError, match="unknown role"):
        d.set_agents([{"name": "x", "role": "bogus", "instructions": ""}])


def test_set_agents_rejects_duplicate_names():
    d = PlanDraft()
    with pytest.raises(AgoraError, match="duplicate agent name"):
        d.set_agents(
            [
                {"name": "x", "role": "architect"},
                {"name": "x", "role": "implementer"},
            ]
        )


def test_set_agents_normalizes_fields():
    d = PlanDraft()
    d.set_agents([{"name": "a", "role": "architect"}])
    assert d.agents == [{"name": "a", "role": "architect", "instructions": "", "model": ""}]


# ---------------------------------------------------------- v2.3 agent fleet


def test_upsert_agent_adds_new_then_updates(draft: PlanDraft):
    """upsert_agent is idempotent on name — second call with same name
    replaces the entry instead of duplicating."""
    d = PlanDraft()
    added_first = d.upsert_agent("a1", "architect", "writes designs", model="gpt-4")
    assert added_first is True
    assert len(d.agents) == 1
    added_second = d.upsert_agent("a1", "architect", "refined instructions", model="gpt-4")
    assert added_second is False  # replaced, not appended
    assert len(d.agents) == 1
    assert d.agents[0]["instructions"] == "refined instructions"


def test_upsert_agent_rejects_bad_role():
    d = PlanDraft()
    with pytest.raises(AgoraError, match="unknown role"):
        d.upsert_agent("a1", "wizard", "cast spells")


def test_upsert_agent_rejects_empty_name():
    d = PlanDraft()
    with pytest.raises(AgoraError, match="non-empty"):
        d.upsert_agent("", "architect", "...")


def test_reset_agents_returns_prior_count(draft: PlanDraft):
    assert len(draft.agents) == 2
    prior = draft.reset_agents()
    assert prior == 2
    assert draft.agents == []


def test_validate_agent_flags_missing_agent(draft: PlanDraft):
    problems = draft.validate_agent("ghost")
    assert any("not in the draft roster" in p for p in problems)


def test_validate_agent_flags_short_instructions():
    d = PlanDraft()
    d.upsert_agent("a1", "architect", "short")
    problems = d.validate_agent("a1")
    assert any("too short" in p for p in problems)


def test_validate_agent_flags_wrong_role():
    d = PlanDraft()
    d.upsert_agent("a1", "architect", "writes architectural designs and outlines")
    problems = d.validate_agent("a1", expected_role="implementer")
    assert any("expected role 'implementer'" in p for p in problems)


def test_validate_agent_passes_valid_agent():
    d = PlanDraft()
    d.upsert_agent(
        "a1", "architect",
        "writes architectural designs. produces module outlines.",
    )
    assert d.validate_agent("a1", expected_role="architect") == []


def test_validate_roster_flags_too_few():
    d = PlanDraft()
    d.upsert_agent("a1", "architect", "writes the architecture documents")
    problems = d.validate_roster()
    assert any("need ≥ 2" in p for p in problems)


def test_validate_roster_flags_no_builder():
    d = PlanDraft()
    d.upsert_agent("r1", "reviewer", "reviews finished work, checks compliance")
    d.upsert_agent("t1", "tester", "writes pytest tests that cover the deliverables")
    problems = d.validate_roster()
    assert any("no builder role" in p for p in problems)


def test_validate_roster_clean():
    d = PlanDraft()
    d.upsert_agent("a1", "architect", "writes the architecture documents")
    d.upsert_agent("i1", "implementer", "implements the CLI and core logic")
    assert d.validate_roster() == []


def test_validate_agents_vs_tasks_flags_orphan_agent(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    problems = draft.validate_agents_vs_tasks()
    assert any("no tasks assigned" in p for p in problems)
    assert "'impl'" in str(problems)


def test_validate_agents_vs_tasks_flags_unknown_assignee(draft: PlanDraft):
    """Direct-dict mutation (bypassing add_task's validation) produces an
    unknown-assignee problem the cross-validator catches."""
    draft.add_task("t1", "d", "arch")
    draft.tasks["t1"]["assigned_to"] = "ghost"
    problems = draft.validate_agents_vs_tasks()
    assert any("not in the agent roster" in p for p in problems)


def test_link_tasks_to_agents_rebalances_orphan():
    """When every task lands on the same agent and one agent is orphaned,
    the linker reassigns a task to fix coverage."""
    d = PlanDraft()
    d.upsert_agent("arch", "architect", "writes architectural designs and contracts")
    d.upsert_agent("impl", "implementer", "implements the cli and core module")
    d.upsert_agent("test", "tester", "writes pytest tests for the cli deliverables")
    d.add_task("setup_project", "Scaffold the package", "impl", output_path="pyproject.toml")
    d.attach_postcondition("setup_project", "mark_complete", {})
    d.add_task("build_cli", "Build the cli entry point", "impl", output_path="src/cli.py")
    d.attach_postcondition("build_cli", "mark_complete", {})
    d.add_task("write_tests", "Write pytest cases", "impl", output_path="tests/test_cli.py")
    d.attach_postcondition("write_tests", "mark_complete", {})

    actions = d.link_tasks_to_agents()
    # The setup-shaped task is an architect match → rebalanced away from impl.
    # The tests-shaped task is a tester match → rebalanced away from impl.
    assert len(actions["rebalanced"]) >= 2
    owners = {t["assigned_to"] for t in d.tasks.values()}
    assert owners == {"arch", "impl", "test"}, owners
    assert d.validate_agents_vs_tasks() == []


def test_link_tasks_to_agents_fills_empty_assigned_to():
    d = PlanDraft()
    d.upsert_agent("arch", "architect", "writes architectural designs and contracts")
    d.upsert_agent("impl", "implementer", "implements the cli and core module")
    # Add a task with NO assigned_to via direct-dict mutation (bypassing add_task's
    # validation) — simulates the "model omitted assigned_to" case.
    d.tasks["core"] = {
        "description": "Implement the hash generation module",
        "assigned_to": "",
        "depends_on": [],
        "output_path": "src/hash.py",
        "postconditions": [{"name": "mark_complete", "args": {}}],
        "stages": [],
    }
    actions = d.link_tasks_to_agents()
    assert "core" in actions["filled"]
    assert d.tasks["core"]["assigned_to"] in {"arch", "impl"}


def test_link_tasks_to_agents_respects_explicit_assignments():
    d = PlanDraft()
    d.upsert_agent("arch", "architect", "writes architectural designs and contracts")
    d.upsert_agent("impl", "implementer", "implements the cli and core module")
    d.add_task("t1", "Build CLI", "arch", output_path="src/cli.py")
    d.attach_postcondition("t1", "mark_complete", {})
    d.add_task("t2", "Build core", "impl", output_path="src/core.py")
    d.attach_postcondition("t2", "mark_complete", {})
    actions = d.link_tasks_to_agents()
    # Both agents already have a task; nothing to fill or rebalance.
    assert actions == {"filled": [], "rebalanced": []}
    assert d.tasks["t1"]["assigned_to"] == "arch"
    assert d.tasks["t2"]["assigned_to"] == "impl"


def test_suggest_role_heuristic_tests_prefer_tester():
    d = PlanDraft()
    assert d._suggest_role({
        "description": "d", "output_path": "tests/test_cli.py", "_id": "write_tests",
    }) == "tester"


def test_suggest_role_heuristic_setup_prefers_architect():
    d = PlanDraft()
    assert d._suggest_role({
        "description": "Scaffold the package skeleton",
        "output_path": "pyproject.toml",
        "_id": "setup_project",
    }) == "architect"


def test_suggest_role_defaults_to_implementer():
    d = PlanDraft()
    assert d._suggest_role({
        "description": "Build the hash generator",
        "output_path": "src/hash.py",
        "_id": "hash_module",
    }) == "implementer"


def test_validate_agents_vs_tasks_clean(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    draft.add_task("t2", "d", "impl")
    assert draft.validate_agents_vs_tasks() == []


def test_snapshot_markdown_includes_agents_and_tasks(draft: PlanDraft):
    draft.add_task("t1", "Build the CLI", "arch", output_path="cli.py")
    draft.attach_postcondition("t1", "mark_complete", {})
    md = draft.snapshot_markdown()
    assert "## Agents (2)" in md
    assert "**arch** (role=architect)" in md
    assert "**impl** (role=implementer)" in md
    assert "## Tasks (1)" in md
    assert "**t1** → arch" in md
    assert "mark_complete" in md


def test_snapshot_markdown_empty_draft():
    md = PlanDraft().snapshot_markdown()
    assert "## Agents (0)" in md
    assert "_(none yet)_" in md


# ---------------------------------------------------------- v2.3 predicates


def test_plan_draft_has_min_agents_predicate():
    from agora.plan.predicate_registry import build_predicate

    d = PlanDraft()
    d.upsert_agent("a1", "architect", "writes architectural outlines clearly")
    d.upsert_agent("i1", "implementer", "implements the core module per outline")
    d.upsert_agent("t1", "tester", "writes pytest tests for the deliverables")
    pred = build_predicate("plan_draft_has_min_agents", {"min_agents": 3})
    ok, _reason = pred.evaluate({"plan_draft": d})
    assert ok is True

    short = PlanDraft()
    short.upsert_agent("a1", "architect", "writes architectural outlines clearly")
    ok2, reason2 = pred.evaluate({"plan_draft": short})
    assert ok2 is False
    assert "1 agents" in reason2


def test_plan_draft_all_agents_valid_predicate():
    from agora.plan.predicate_registry import build_predicate

    pred = build_predicate("plan_draft_all_agents_valid", {})

    d = PlanDraft()
    d.upsert_agent("a1", "architect", "writes architectural outlines clearly")
    ok, _ = pred.evaluate({"plan_draft": d})
    assert ok is True

    bad = PlanDraft()
    bad.upsert_agent("a1", "architect", "x")  # too short
    ok2, reason2 = pred.evaluate({"plan_draft": bad})
    assert ok2 is False
    assert "too short" in reason2


# ---------------------------------------------------------- round-trip validation_args


def test_stagetemplate_validation_args_roundtrip(tmp_path):
    """``validation_args`` survives save_flow + load_flow round-trip."""
    from agora.core.agent import AgentConfig
    from agora.core.flow import Flow, StageTemplate, TaskTemplate, load_flow, save_flow
    from agora.core.types import AgentRole

    flow = Flow(
        name="rt",
        description="",
        agents=(AgentConfig(name="arch", role=AgentRole.ARCHITECT),),
        task_graph=(
            TaskTemplate(
                id="t1",
                assigned_to="arch",
                description="d",
                output_path="",
                stages=(
                    StageTemplate(
                        name="validate",
                        kind="plan_validate_agent",
                        validation_args=(
                            ("expected_name", "arch"),
                            ("expected_role", "architect"),
                        ),
                    ),
                ),
            ),
        ),
    )
    path = tmp_path / "rt.plan.yaml"
    save_flow(flow, path)
    reloaded = load_flow(path)
    st = reloaded.task_graph[0].stages[0]
    assert st.kind == "plan_validate_agent"
    assert dict(st.validation_args) == {
        "expected_name": "arch",
        "expected_role": "architect",
    }


# ----------------------------------------------------------------------- tasks


def test_add_task_requires_agents_first():
    d = PlanDraft()
    with pytest.raises(AgoraError, match="plan_set_agents must be called"):
        d.add_task("t", "desc", "arch")


def test_add_task_rejects_unknown_agent(draft: PlanDraft):
    with pytest.raises(AgoraError, match="not a registered agent"):
        draft.add_task("t1", "desc", "nope")


def test_add_task_rejects_duplicate_id(draft: PlanDraft):
    draft.add_task("t1", "desc", "arch")
    with pytest.raises(AgoraError, match="already exists"):
        draft.add_task("t1", "desc", "arch")


def test_add_task_rejects_missing_dep(draft: PlanDraft):
    with pytest.raises(AgoraError, match="depends on"):
        draft.add_task("t1", "desc", "arch", depends_on=["not_added"])


def test_add_task_spec_atomic_success(draft: PlanDraft):
    attached = draft.add_task_spec(
        "t1", "d", "arch", output_path="kb/x.md",
        postconditions=[
            {"name": "file_exists", "args": {"rel": "kb/x.md"}},
            {"name": "mark_complete", "args": {}},
        ],
    )
    assert attached == 2
    assert "t1" in draft.tasks
    assert [pc["name"] for pc in draft.tasks["t1"]["postconditions"]] == [
        "file_exists", "mark_complete"
    ]


def test_add_task_spec_rolls_back_on_bad_postcondition(draft: PlanDraft):
    """A malformed postcondition must leave the draft untouched — no
    half-authored task remains."""
    with pytest.raises(AgoraError):
        draft.add_task_spec(
            "t1", "d", "arch",
            postconditions=[
                # file_contains requires substring; missing → factory raises.
                {"name": "file_contains", "args": {"rel": "x.md"}},
            ],
        )
    assert "t1" not in draft.tasks


def test_add_task_spec_requires_known_agent(draft: PlanDraft):
    with pytest.raises(AgoraError, match="not a registered agent"):
        draft.add_task_spec(
            "t1", "d", "ghost",
            postconditions=[{"name": "mark_complete", "args": {}}],
        )


# ------------------------------------------------------- postcondition autofill


def test_add_task_spec_autofills_rel_from_output_path(draft: PlanDraft):
    """The 7B routinely omits nested ``args.rel`` — the framework backfills
    it from ``output_path`` for factories that need it."""
    draft.add_task_spec(
        "t1", "write the cli", "arch",
        output_path="src/cli.py",
        postconditions=[
            {"name": "file_exists"},       # no args
            {"name": "py_compiles", "args": {}},  # empty args dict
            {"name": "mark_complete"},
        ],
    )
    pcs = draft.tasks["t1"]["postconditions"]
    by_name = {pc["name"]: pc["args"] for pc in pcs}
    assert by_name["file_exists"] == {"rel": "src/cli.py"}
    assert by_name["py_compiles"] == {"rel": "src/cli.py"}
    assert by_name["mark_complete"] == {}


def test_add_task_spec_respects_explicit_rel(draft: PlanDraft):
    """Explicit ``rel`` is never overwritten — the framework only fills
    missing values."""
    draft.add_task_spec(
        "t1", "build", "arch",
        output_path="src/cli.py",
        postconditions=[
            {"name": "file_exists", "args": {"rel": "requirements.txt"}},
        ],
    )
    assert draft.tasks["t1"]["postconditions"][0]["args"] == {
        "rel": "requirements.txt"
    }


def test_add_task_spec_skips_autofill_without_output_path(draft: PlanDraft):
    """Without an ``output_path`` the framework has nothing to infer from,
    so the factory's own missing-arg error reaches the caller unchanged —
    the model can see the catalog and pass rel explicitly next time."""
    with pytest.raises(AgoraError, match="args invalid"):
        draft.add_task_spec(
            "t1", "d", "arch",
            output_path="",  # nothing to infer from
            postconditions=[{"name": "file_exists"}],
        )
    assert "t1" not in draft.tasks


def test_add_task_spec_leaves_multi_arg_factories_to_model(draft: PlanDraft):
    """file_contains takes rel AND substring. Rel is autofilled; substring
    still has to come from the caller or the factory rejects."""
    with pytest.raises(AgoraError, match="args invalid"):
        draft.add_task_spec(
            "t1", "d", "arch",
            output_path="README.md",
            postconditions=[{"name": "file_contains"}],  # missing substring
        )
    assert "t1" not in draft.tasks

    draft.add_task_spec(
        "t2", "d", "arch",
        output_path="README.md",
        postconditions=[
            {"name": "file_contains", "args": {"substring": "## Usage"}},
        ],
    )
    # rel got autofilled; substring came from caller.
    assert draft.tasks["t2"]["postconditions"][0]["args"] == {
        "rel": "README.md",
        "substring": "## Usage",
    }


def test_add_task_spec_rejects_non_dict_pc(draft: PlanDraft):
    with pytest.raises(AgoraError, match="must be a dict"):
        draft.add_task_spec(
            "t1", "d", "arch", postconditions=["mark_complete"]  # type: ignore[list-item]
        )
    assert "t1" not in draft.tasks


def test_add_task_spec_rejects_non_dict_args(draft: PlanDraft):
    with pytest.raises(AgoraError, match="args must be an object"):
        draft.add_task_spec(
            "t1", "d", "arch",
            postconditions=[{"name": "mark_complete", "args": "nope"}],  # type: ignore[typeddict-item]
        )
    assert "t1" not in draft.tasks


def test_add_decision_stage_rejects_empty_name(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    with pytest.raises(AgoraError, match="stage name"):
        draft.add_decision_stage("t1", "", "d1", "Q?", ["a", "b"], "p.txt")


def test_add_decision_stage_rejects_unknown_task(draft: PlanDraft):
    with pytest.raises(AgoraError, match="unknown task_id"):
        draft.add_decision_stage("ghost", "s", "d1", "Q?", ["a", "b"], "p.txt")


def test_add_decision_stage_rejects_missing_question(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    with pytest.raises(AgoraError, match="requires question"):
        draft.add_decision_stage("t1", "s", "d1", "", ["a", "b"], "p.txt")


def test_validate_ready_catches_missing_dependency(draft: PlanDraft):
    """If somebody hand-mutates ``tasks`` to reference a missing dep,
    ``validate_ready`` surfaces it via the DAG builder."""
    draft.add_task("t1", "d", "arch")
    draft.attach_postcondition("t1", "mark_complete", {})
    # Simulate a broken state: patch depends_on to a nonexistent id.
    draft.tasks["t1"]["depends_on"] = ["ghost"]
    problems = draft.validate_ready()
    assert any("invalid DAG" in p for p in problems), problems


def test_add_task_preserves_order(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    draft.add_task("t2", "d", "arch", depends_on=["t1"])
    draft.add_task("t3", "d", "impl", depends_on=["t2"])
    assert list(draft.tasks) == ["t1", "t2", "t3"]


# ---------------------------------------------------------- postconditions


def test_attach_postcondition_rejects_unknown_task(draft: PlanDraft):
    with pytest.raises(AgoraError, match="unknown task_id"):
        draft.attach_postcondition("ghost", "mark_complete")


def test_attach_postcondition_rejects_unknown_predicate(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    with pytest.raises(AgoraError):  # unknown predicate surfaces from registry
        draft.attach_postcondition("t1", "not_a_real_predicate")


def test_attach_postcondition_rejects_wrong_args(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    # file_contains requires substring — missing arg triggers TypeError in factory
    with pytest.raises(AgoraError, match="args invalid"):
        draft.attach_postcondition("t1", "file_contains", {"rel": "x.md"})


def test_attach_postcondition_appends(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    draft.attach_postcondition("t1", "mark_complete", {})
    draft.attach_postcondition("t1", "file_exists", {"rel": "x.md"})
    pcs = draft.tasks["t1"]["postconditions"]
    assert [pc["name"] for pc in pcs] == ["mark_complete", "file_exists"]
    assert pcs[1]["args"] == {"rel": "x.md"}


# ----------------------------------------------------------------- stages


def test_add_llm_stage_happy_path(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    draft.add_llm_stage("t1", "write", "do the thing", max_iterations=3)
    s = draft.tasks["t1"]["stages"][0]
    assert s["kind"] == "llm"
    assert s["max_iterations"] == 3


def test_add_llm_stage_rejects_duplicate_name(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    draft.add_llm_stage("t1", "write", "x")
    with pytest.raises(AgoraError, match="already has a stage named"):
        draft.add_llm_stage("t1", "write", "y")


def test_add_decision_stage_happy_path(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    draft.add_decision_stage(
        "t1", "ask_x", "decision_x", "Which?", ["a", "b"], "plan/ans.txt"
    )
    s = draft.tasks["t1"]["stages"][0]
    assert s["kind"] == "decision"
    assert s["decision_id"] == "decision_x"


def test_add_decision_stage_rejects_duplicate_decision_id(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    draft.add_task("t2", "d", "arch")
    draft.add_decision_stage(
        "t1", "s", "dup", "Q?", ["a", "b"], "plan/a.txt"
    )
    with pytest.raises(AgoraError, match="already used"):
        draft.add_decision_stage(
            "t2", "s", "dup", "Q2?", ["c", "d"], "plan/b.txt"
        )


def test_add_decision_stage_requires_two_options(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    with pytest.raises(AgoraError, match="≥ 2 options"):
        draft.add_decision_stage("t1", "s", "d1", "Q?", ["only"], "p.txt")


def test_add_decision_stage_requires_output_path(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    with pytest.raises(AgoraError, match="output_path"):
        draft.add_decision_stage("t1", "s", "d1", "Q?", ["a", "b"], "")


# --------------------------------------------------------------- readiness


def test_validate_ready_flags_missing_agents():
    d = PlanDraft()
    problems = d.validate_ready()
    assert any("no agents" in p for p in problems)


def test_validate_ready_flags_missing_postconditions(draft: PlanDraft):
    """Strict gate: every task must carry ≥ 1 postcondition. The compound
    ``plan_add_task_spec`` tool makes this trivial to satisfy — each per-task
    author stage attaches postconditions in the same call that adds the task,
    so weak-model planners don't have to loop across a growing task list."""
    draft.add_task("t1", "d", "arch")
    problems = draft.validate_ready()
    assert any("without postconditions" in p for p in problems), problems


def test_validate_ready_clean(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    draft.attach_postcondition("t1", "mark_complete", {})
    assert draft.validate_ready() == []


# ----------------------------------------------------- to_flow + round-trip


def test_to_flow_produces_valid_flow(draft: PlanDraft):
    draft.add_task("t1", "d", "arch")
    draft.attach_postcondition("t1", "mark_complete", {})
    flow = draft.to_flow()
    assert isinstance(flow, Flow)
    assert flow.name == "test-plan"
    assert len(flow.agents) == 2
    assert len(flow.task_graph) == 1
    assert flow.task_graph[0].postconditions[0].name == "mark_complete"


def test_draft_round_trip_via_save_and_load(tmp_path, draft: PlanDraft):
    """Full loop: build a draft → save_flow → load_plan → instantiate_plan."""
    draft.add_task("fetch", "fetch the thing", "arch", output_path="kb/x.md")
    draft.attach_postcondition("fetch", "file_exists", {"rel": "kb/x.md"})
    draft.attach_postcondition("fetch", "mark_complete", {})
    draft.add_task("build", "build it", "impl", depends_on=["fetch"], output_path="out.py")
    draft.attach_postcondition("build", "file_exists", {"rel": "out.py"})
    draft.attach_postcondition("build", "mark_complete", {})
    draft.add_llm_stage(
        "build", "write_out", "write the file", context_files=["kb/x.md"], max_iterations=4
    )

    assert draft.validate_ready() == []
    flow = draft.to_flow()
    out_path = tmp_path / "out.plan.yaml"
    save_flow(flow, out_path)

    # Reload and confirm the round-trip gives us the same structure.
    plan = load_plan(out_path)
    agents, tasks, staged = instantiate_plan(plan, project_name="rt")
    assert [a.name for a in agents] == ["arch", "impl"]
    assert [t.id for t in tasks] == ["fetch", "build"]
    assert "build" in staged
    assert len(staged["build"].stages) == 1
    # Postconditions materialised via registry.
    build_task = next(t for t in tasks if t.id == "build")
    pc_names = [p.name for p in build_task.spec.postconditions]
    assert "artifact_contains_out.py" in pc_names
    assert "mark_complete_called" in pc_names


# ================================================================
# Approach C (C4a): src/*.py output_path validation against api_spec
# ================================================================


def test_add_task_spec_accepts_src_path_in_api_spec(draft: PlanDraft):
    """A task targeting a src/*.py module that IS declared in api_spec
    sails through unchanged."""
    draft.add_task_spec(
        "t1", "impl", "arch",
        output_path="src/url_shortener.py",
        postconditions=[
            {"name": "file_exists"},
            {"name": "py_compiles"},
            {"name": "mark_complete"},
        ],
        api_spec_modules={"src/url_shortener.py"},
    )
    assert "t1" in draft.tasks


def test_add_task_spec_rejects_src_path_not_in_api_spec(draft: PlanDraft):
    """output_path='src/cli.py' with api_spec only declaring
    src/url_shortener.py → fail with structured message."""
    with pytest.raises(AgoraError) as excinfo:
        draft.add_task_spec(
            "t1", "impl", "arch",
            output_path="src/cli.py",
            postconditions=[
                {"name": "file_exists"},
                {"name": "py_compiles"},
                {"name": "mark_complete"},
            ],
            api_spec_modules={"src/url_shortener.py"},
        )
    msg = str(excinfo.value)
    # Error names the offending task, the bad path, and known modules.
    assert "t1" in msg
    assert "src/cli.py" in msg
    assert "src/url_shortener.py" in msg
    assert "fix:" in msg.lower()
    # Draft is left untouched — the rejected task never made it in.
    assert "t1" not in draft.tasks


def test_add_task_spec_rejects_postcondition_rel_not_in_api_spec(draft: PlanDraft):
    """Even when output_path is fine, a py_compiles postcondition pointing
    at a src/*.py outside the spec gets rejected."""
    with pytest.raises(AgoraError) as excinfo:
        draft.add_task_spec(
            "t1", "impl", "arch",
            output_path="",  # no output_path
            postconditions=[
                {"name": "py_compiles", "args": {"rel": "src/cli.py"}},
                {"name": "mark_complete"},
            ],
            api_spec_modules={"src/url_shortener.py"},
        )
    msg = str(excinfo.value)
    assert "src/cli.py" in msg
    assert "t1" not in draft.tasks


def test_add_task_spec_skips_validation_without_api_spec(draft: PlanDraft):
    """api_spec_modules=None (no spec authored yet) → no validation.
    Back-compat: callers that don't supply the set get the old behaviour."""
    draft.add_task_spec(
        "t1", "impl", "arch",
        output_path="src/anything.py",
        postconditions=[
            {"name": "file_exists"},
            {"name": "py_compiles"},
            {"name": "mark_complete"},
        ],
        # api_spec_modules omitted → defaults to None
    )
    assert "t1" in draft.tasks


def test_add_task_spec_skips_validation_with_empty_api_spec(draft: PlanDraft):
    """Empty set (file exists but no modules parsed) = skip validation —
    the api_spec_is_valid predicate gates that case independently."""
    draft.add_task_spec(
        "t1", "impl", "arch",
        output_path="src/anything.py",
        postconditions=[{"name": "mark_complete"}],
        api_spec_modules=set(),
    )
    assert "t1" in draft.tasks


def test_add_task_spec_allows_non_src_paths_regardless(draft: PlanDraft):
    """Non-src paths (requirements.txt, tests/*, README.md) are never
    gated against the api_spec — they're not production modules."""
    draft.add_task_spec(
        "setup", "scaffold", "arch",
        output_path="requirements.txt",
        postconditions=[{"name": "file_exists"}, {"name": "mark_complete"}],
        api_spec_modules={"src/url_shortener.py"},
    )
    draft.add_task_spec(
        "tests", "test", "arch",
        output_path="tests/test_x.py",
        postconditions=[{"name": "file_exists"}, {"name": "mark_complete"}],
        api_spec_modules={"src/url_shortener.py"},
    )
    assert "setup" in draft.tasks
    assert "tests" in draft.tasks


def test_add_task_spec_allows_src_tests_path(draft: PlanDraft):
    """src/tests/* looks like a src path but is actually a test path —
    not validated against api_spec (same reasoning as tests/*)."""
    draft.add_task_spec(
        "t1", "test", "arch",
        output_path="src/tests/test_x.py",
        postconditions=[{"name": "file_exists"}, {"name": "mark_complete"}],
        api_spec_modules={"src/url_shortener.py"},
    )
    assert "t1" in draft.tasks


def test_add_task_spec_reports_multiple_bad_refs(draft: PlanDraft):
    """If BOTH output_path AND a postcondition's rel are bad, the message
    names both so the model sees the complete violation set."""
    with pytest.raises(AgoraError) as excinfo:
        draft.add_task_spec(
            "t1", "impl", "arch",
            output_path="src/bad1.py",
            postconditions=[
                {"name": "py_compiles", "args": {"rel": "src/bad2.py"}},
                {"name": "mark_complete"},
            ],
            api_spec_modules={"src/url_shortener.py"},
        )
    msg = str(excinfo.value)
    assert "src/bad1.py" in msg
    assert "src/bad2.py" in msg


# ================================================================
# v2.9 (C4a tightened): catch non-src Python paths too — plan/*.py
# is a category error the prior src-only filter let through.
# ================================================================


def test_add_task_spec_rejects_plan_dir_output_path(draft: PlanDraft):
    """`plan/` is the planner's own workspace — never a valid executor
    output_path. Regression guard for the 2026-04-22 run where the
    architect emitted ``output_path: plan/core_domain_module.py`` and
    the old src-only filter let it through silently."""
    with pytest.raises(AgoraError) as excinfo:
        draft.add_task_spec(
            "core_domain_module", "impl core", "impl",
            output_path="plan/core_domain_module.py",
            postconditions=[
                {"name": "file_exists"},
                {"name": "py_compiles"},
                {"name": "mark_complete"},
            ],
            api_spec_modules={"src/url_shortener.py"},
        )
    msg = str(excinfo.value)
    assert "plan/core_domain_module.py" in msg
    # The fix hint must explicitly warn about plan/ being a category error.
    assert "plan/" in msg and "workspace" in msg.lower()


def test_add_task_spec_rejects_arbitrary_dir_python_path(draft: PlanDraft):
    """Even non-src, non-plan paths like ``scripts/run.py`` get rejected
    when not in api_spec — there's no legitimate reason for impl tasks
    to target files outside api_spec_modules."""
    with pytest.raises(AgoraError):
        draft.add_task_spec(
            "t1", "impl", "impl",
            output_path="scripts/run.py",
            postconditions=[
                {"name": "py_compiles", "args": {"rel": "scripts/run.py"}},
                {"name": "mark_complete"},
            ],
            api_spec_modules={"src/url_shortener.py"},
        )


def test_add_task_spec_still_accepts_test_paths(draft: PlanDraft):
    """Tests live under ``tests/`` — they're scaffolded separately and
    have no api_spec entry. C4a must NOT flag them."""
    draft.add_task_spec(
        "pytest_tests", "write tests", "arch",
        output_path="tests/test_url_shortener.py",
        postconditions=[
            {"name": "file_exists"},
            {"name": "pytest_passes", "args": {"rel": "tests/"}},
            {"name": "mark_complete"},
        ],
        api_spec_modules={"src/url_shortener.py"},
    )
    assert "pytest_tests" in draft.tasks


def test_add_task_spec_still_accepts_non_python_artifacts(draft: PlanDraft):
    """requirements.txt, README.md, etc. are artifacts but not Python
    modules — they aren't subject to the api_spec check."""
    draft.add_task_spec(
        "setup", "scaffold", "arch",
        output_path="requirements.txt",
        postconditions=[
            {"name": "file_exists"},
            {"name": "mark_complete"},
        ],
        api_spec_modules={"src/url_shortener.py"},
    )
    assert "setup" in draft.tasks


def test_add_task_spec_still_accepts_spec_matching_src_path(draft: PlanDraft):
    """Happy path preserved: when output_path matches an api_spec module,
    the task goes through."""
    draft.add_task_spec(
        "core", "impl core module", "impl",
        output_path="src/url_shortener.py",
        postconditions=[
            {"name": "file_exists"},
            {"name": "py_compiles"},
            {"name": "mark_complete"},
        ],
        api_spec_modules={"src/url_shortener.py"},
    )
    assert "core" in draft.tasks
