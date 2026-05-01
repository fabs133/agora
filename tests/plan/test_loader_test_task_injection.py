"""Covers the v2.4 loader transform that wraps test-authoring tasks with
a scaffold + llm + verify 3-stage pipeline at ``instantiate_plan`` time."""

from __future__ import annotations

from pathlib import Path

from agora.core.agent import AgentConfig
from agora.core.flow import (
    Flow,
    PostconditionRef,
    StageTemplate,
    TaskTemplate,
)
from agora.core.types import AgentRole
from agora.plan.loader import (
    Plan,
    _is_test_authoring_task,
    _split_test_task,
    instantiate_plan,
)


def _agent(name: str = "tester", role: AgentRole = AgentRole.TESTER) -> AgentConfig:
    return AgentConfig(name=name, role=role)


def _task(
    id_: str = "implement_tests",
    assigned_to: str = "tester",
    output_path: str = "",
    postconditions: tuple[PostconditionRef, ...] = (),
    stages: tuple[StageTemplate, ...] = (),
) -> TaskTemplate:
    return TaskTemplate(
        id=id_,
        assigned_to=assigned_to,
        description="write tests",
        depends_on=(),
        output_path=output_path,
        postconditions=postconditions,
        stages=stages,
    )


# ---------------------------------------------------------------- _is_test_authoring_task


def test_is_test_authoring_task_via_output_path():
    t = _task(
        output_path="tests/test_cli.py",
        postconditions=(
            PostconditionRef(name="pytest_passes", args=()),
            PostconditionRef(name="mark_complete", args=()),
        ),
    )
    assert _is_test_authoring_task(t) is True


def test_is_test_authoring_task_via_file_exists_postcondition_rel():
    """Plan-builder sometimes leaves output_path empty and encodes the test
    path as file_exists.args.rel — the detector should still match."""
    t = _task(
        output_path="",
        postconditions=(
            PostconditionRef(
                name="file_exists",
                args=(("rel", "tests/test_cli.py"),),
            ),
            PostconditionRef(name="pytest_passes", args=()),
        ),
    )
    assert _is_test_authoring_task(t) is True


def test_is_test_authoring_task_requires_pytest_passes():
    t = _task(
        output_path="tests/conftest.py",
        postconditions=(
            PostconditionRef(name="file_exists", args=(("rel", "tests/conftest.py"),)),
        ),
    )
    # No pytest_passes → not a test-authoring task (probably a fixture module).
    assert _is_test_authoring_task(t) is False


def test_is_test_authoring_task_skips_if_stages_present():
    """A task that already declares stages is respected as-is."""
    t = _task(
        output_path="tests/test_cli.py",
        postconditions=(PostconditionRef(name="pytest_passes", args=()),),
        stages=(StageTemplate(name="explicit", kind="llm", instruction="x"),),
    )
    assert _is_test_authoring_task(t) is False


def test_is_test_authoring_task_skips_non_tests_path():
    t = _task(
        output_path="src/main.py",
        postconditions=(PostconditionRef(name="pytest_passes", args=()),),
    )
    assert _is_test_authoring_task(t) is False


# ------------------------------------------------------------------ _wrap_test_task


def test_split_test_task_produces_contract_and_impl():
    """v2.5 splits a single emitted test task into 2 templates:
    <id>_contract (runs early, no pytest verify) and <id> (runs after
    implementers, keeps verify)."""
    from agora.plan.loader import _split_test_task

    t = _task(
        id_="implement_tests",
        output_path="tests/test_cli.py",
        postconditions=(PostconditionRef(name="pytest_passes", args=()),),
    )
    split = _split_test_task(t)
    assert len(split) == 2
    contract, impl = split
    assert contract.id == "implement_tests_contract"
    assert impl.id == "implement_tests"
    # v2.6 adds derive_intent stage between scaffold and fill_assertions.
    # Contract: scaffold + derive_intent + llm (no verify; impl missing).
    contract_kinds = [s.kind for s in contract.stages]
    assert contract_kinds == [
        "plan_scaffold_tests",
        "plan_derive_test_intent",
        "llm",
    ]
    # Impl: scaffold + derive_intent + llm + verify.
    impl_kinds = [s.kind for s in impl.stages]
    assert impl_kinds == [
        "plan_scaffold_tests",
        "plan_derive_test_intent",
        "llm",
        "plan_run_pytest",
    ]
    # Contract stage's mode is "contract"; impl's is "impl".
    assert dict(contract.stages[0].validation_args).get("mode") == "contract"
    assert dict(impl.stages[0].validation_args).get("mode") == "impl"
    # derive_intent stages point at the scaffold file via validation_args.
    assert (
        dict(contract.stages[1].validation_args).get("scaffold_path")
        == "tests/test_contract.py"
    )
    assert (
        dict(impl.stages[1].validation_args).get("scaffold_path")
        == "tests/test_cli.py"
    )
    # Impl depends on contract.
    assert "implement_tests_contract" in impl.depends_on
    # Contract writes to tests/test_contract.py; impl keeps original path.
    assert contract.stages[0].output_path == "tests/test_contract.py"
    assert impl.stages[0].output_path == "tests/test_cli.py"
    # Sprint 7.1: both tasks gain tests_have_assertions on their file.
    contract_assert_gates = [
        pc for pc in contract.postconditions
        if pc.name == "tests_have_assertions"
        and dict(pc.args).get("rel") == "tests/test_contract.py"
    ]
    assert len(contract_assert_gates) == 1
    impl_assert_gates = [
        pc for pc in impl.postconditions
        if pc.name == "tests_have_assertions"
        and dict(pc.args).get("rel") == "tests/test_cli.py"
    ]
    assert len(impl_assert_gates) == 1


def test_split_test_task_emits_one_impl_per_test_file():
    """v2.7: a task with multiple file_exists(tests/*.py) emits one impl
    task per file, each with its own scaffold + intent + fill + verify."""
    from agora.plan.loader import _split_test_task

    t = _task(
        id_="pytest_coverage",
        output_path="",
        postconditions=(
            PostconditionRef(name="file_exists", args=(("rel", "tests/test_add.py"),)),
            PostconditionRef(name="file_exists", args=(("rel", "tests/test_lookup.py"),)),
            PostconditionRef(name="file_exists", args=(("rel", "tests/test_list.py"),)),
            PostconditionRef(name="pytest_passes", args=(("rel", "tests/"),)),
            PostconditionRef(name="mark_complete", args=()),
        ),
    )
    split = _split_test_task(t)
    # 1 contract + 3 impl.
    assert len(split) == 4
    contract, *impls = split
    assert contract.id == "pytest_coverage_contract"
    # Each impl has a distinct id (suffixed by stem).
    impl_ids = [i.id for i in impls]
    assert impl_ids == [
        "pytest_coverage_test_add",
        "pytest_coverage_test_lookup",
        "pytest_coverage_test_list",
    ]
    # Each impl owns ONE test file.
    assert [i.output_path for i in impls] == [
        "tests/test_add.py",
        "tests/test_lookup.py",
        "tests/test_list.py",
    ]
    # Each impl's pytest_passes is scoped to its own file (not the broad tests/).
    for impl, expected_path in zip(
        impls,
        ["tests/test_add.py", "tests/test_lookup.py", "tests/test_list.py"],
        strict=True,
    ):
        pytest_gates = [
            pc for pc in impl.postconditions
            if pc.name == "pytest_passes"
        ]
        assert len(pytest_gates) == 1
        assert dict(pytest_gates[0].args).get("rel") == expected_path
    # Each impl has a distinct intent output path so siblings don't clobber.
    intent_paths = [
        dict(impl.stages[1].validation_args).get("scaffold_path")
        for impl in impls
    ]
    assert intent_paths == [
        "tests/test_add.py",
        "tests/test_lookup.py",
        "tests/test_list.py",
    ]
    intent_outputs = [impl.stages[1].output_path for impl in impls]
    assert len(set(intent_outputs)) == 3, f"intent paths must be distinct: {intent_outputs}"


def test_split_test_task_rejects_directory_as_output():
    """Regression guard: plan-builder emitted output_path='tests/' (a
    directory). The loader must NOT treat it as a valid test file or the
    scaffolder tries to write to a directory and crashes."""
    from agora.plan.loader import _test_file_paths

    t = _task(
        id_="pytest_tests",
        output_path="tests/",
        postconditions=(
            PostconditionRef(name="pytest_passes", args=(("rel", "tests/"),)),
        ),
    )
    paths = _test_file_paths(t)
    assert paths == []  # "tests/" is not a .py file


def test_split_test_task_normalizes_bare_test_name():
    """Plan-builder sometimes emits ``rel: test_add.py`` without the
    ``tests/`` prefix. The loader auto-prepends so downstream paths resolve."""
    from agora.plan.loader import _test_file_paths

    t = _task(
        id_="pytest_tests",
        output_path="",
        postconditions=(
            PostconditionRef(name="file_exists", args=(("rel", "test_add.py"),)),
            PostconditionRef(name="file_exists", args=(("rel", "test_lookup.py"),)),
            PostconditionRef(name="pytest_passes", args=(("rel", "tests/"),)),
        ),
    )
    paths = _test_file_paths(t)
    assert paths == ["tests/test_add.py", "tests/test_lookup.py"]


def test_split_test_task_single_file_keeps_original_task_id():
    """Back-compat: single-file case preserves the original task id (no suffix)."""
    from agora.plan.loader import _split_test_task

    t = _task(
        id_="implement_tests",
        output_path="tests/test_cli.py",
        postconditions=(PostconditionRef(name="pytest_passes", args=()),),
    )
    split = _split_test_task(t)
    assert len(split) == 2
    contract, impl = split
    assert impl.id == "implement_tests"  # not implement_tests_test_cli


def test_split_test_task_does_not_duplicate_existing_assertion_gate():
    """If the plan already declared tests_have_assertions on the impl path,
    splitting should not add a second copy."""
    from agora.plan.loader import _split_test_task

    t = _task(
        id_="implement_tests",
        output_path="tests/test_cli.py",
        postconditions=(
            PostconditionRef(name="pytest_passes", args=()),
            PostconditionRef(
                name="tests_have_assertions",
                args=(("rel", "tests/test_cli.py"),),
            ),
        ),
    )
    _, impl = _split_test_task(t)
    gates = [
        pc for pc in impl.postconditions
        if pc.name == "tests_have_assertions"
        and dict(pc.args).get("rel") == "tests/test_cli.py"
    ]
    assert len(gates) == 1


def test_split_test_task_derives_path_from_file_exists():
    from agora.plan.loader import _split_test_task

    t = _task(
        id_="tests",
        output_path="",
        postconditions=(
            PostconditionRef(
                name="file_exists", args=(("rel", "tests/test_foo.py"),)
            ),
            PostconditionRef(name="pytest_passes", args=()),
        ),
    )
    split = _split_test_task(t)
    assert split[1].stages[0].output_path == "tests/test_foo.py"


def test_annotate_code_tasks_adds_contract_gate():
    """Code tasks (with py_compiles postcondition) gain a dep on the
    contract task + a pytest_passes(tests/test_contract.py) postcondition."""
    from agora.plan.loader import _annotate_code_tasks_with_contract_gate

    code_task = _task(
        id_="implement_core_module",
        output_path="src/domain.py",
        postconditions=(
            PostconditionRef(name="file_exists", args=(("rel", "src/domain.py"),)),
            PostconditionRef(name="py_compiles", args=(("rel", "src/domain.py"),)),
            PostconditionRef(name="mark_complete", args=()),
        ),
    )
    setup = _task(
        id_="setup_project",
        output_path="src/__init__.py",
        postconditions=(PostconditionRef(name="mark_complete", args=()),),
    )
    result = _annotate_code_tasks_with_contract_gate(
        [setup, code_task], contract_id="implement_tests_contract"
    )
    # setup_project has no py_compiles → unchanged.
    assert result[0].depends_on == ()
    assert all(
        pc.name != "pytest_passes" for pc in result[0].postconditions
    )
    # code task gets annotated.
    core = result[1]
    assert "implement_tests_contract" in core.depends_on
    gate_pcs = [
        pc for pc in core.postconditions
        if pc.name == "pytest_passes"
        and dict(pc.args).get("rel") == "tests/test_contract.py"
    ]
    assert len(gate_pcs) == 1


def test_annotate_is_idempotent():
    """Re-annotating a task that already has the gate doesn't duplicate it."""
    from agora.plan.loader import _annotate_code_tasks_with_contract_gate

    core = _task(
        id_="core",
        output_path="src/x.py",
        postconditions=(
            PostconditionRef(name="py_compiles", args=(("rel", "src/x.py"),)),
            PostconditionRef(
                name="pytest_passes",
                args=(("rel", "tests/test_contract.py"),),
            ),
        ),
    )
    result = _annotate_code_tasks_with_contract_gate(
        [core], contract_id="contract"
    )
    gate_pcs = [
        pc for pc in result[0].postconditions
        if pc.name == "pytest_passes"
        and dict(pc.args).get("rel") == "tests/test_contract.py"
    ]
    assert len(gate_pcs) == 1  # not doubled


# ---------------------------------------------------------- end-to-end instantiate_plan


def test_instantiate_plan_applies_transform_to_test_task(tmp_path: Path):
    """Full end-to-end: a plan containing a test-authoring task gets the
    3-stage wrapper through instantiate_plan."""
    plan = Plan(
        flow=Flow(
            name="rt",
            description="",
            agents=(
                _agent("dev", AgentRole.IMPLEMENTER),
                _agent("tester", AgentRole.TESTER),
            ),
            task_graph=(
                _task(
                    id_="build",
                    assigned_to="dev",
                    output_path="src/thing.py",
                    postconditions=(
                        PostconditionRef(name="mark_complete", args=()),
                    ),
                ),
                _task(
                    id_="tests",
                    assigned_to="tester",
                    output_path="",
                    postconditions=(
                        PostconditionRef(
                            name="file_exists",
                            args=(("rel", "tests/test_thing.py"),),
                        ),
                        PostconditionRef(name="pytest_passes", args=()),
                        PostconditionRef(name="mark_complete", args=()),
                    ),
                ),
            ),
        )
    )
    agents, tasks, staged = instantiate_plan(plan, project_name="rt")
    # v2.5: split produces 2 test tasks (contract + impl) + the original code
    # task = 3 tasks. Both test tasks get staged.
    assert len(tasks) == 3
    assert set(staged) == {"tests_contract", "tests"}
    # v2.6: scaffold + derive_intent + llm (contract has no verify).
    contract_stages = staged["tests_contract"].stages
    assert [s.kind for s in contract_stages] == [
        "plan_scaffold_tests",
        "plan_derive_test_intent",
        "llm",
    ]
    # Impl: scaffold + derive_intent + llm + verify.
    impl_stages = staged["tests"].stages
    assert [s.kind for s in impl_stages] == [
        "plan_scaffold_tests",
        "plan_derive_test_intent",
        "llm",
        "plan_run_pytest",
    ]


def test_instantiate_plan_leaves_non_test_tasks_alone(tmp_path: Path):
    plan = Plan(
        flow=Flow(
            name="rt",
            description="",
            agents=(_agent("dev", AgentRole.IMPLEMENTER),),
            task_graph=(
                _task(
                    id_="build",
                    assigned_to="dev",
                    output_path="src/thing.py",
                    postconditions=(
                        PostconditionRef(name="mark_complete", args=()),
                    ),
                ),
            ),
        )
    )
    _, _, staged = instantiate_plan(plan, project_name="rt")
    assert staged == {}


def test_split_test_task_synthesizes_fallback_brief_from_description():
    """The loader passes the task description's deliverables list through
    validation_args (both contract + impl tasks) so the scaffolder can emit
    per-deliverable stubs even without plan/brief.md in the executor workspace."""

    t = _task(
        id_="tests",
        output_path="tests/test_x.py",
        postconditions=(PostconditionRef(name="pytest_passes", args=()),),
    )
    t = t.__class__(
        id=t.id, assigned_to=t.assigned_to,
        description=(
            "pytest tests covering the deliverables from the brief "
            "(add/lookup/list plus edge cases)"
        ),
        depends_on=t.depends_on, output_path=t.output_path,
        postconditions=t.postconditions, stages=t.stages,
    )
    contract, impl = _split_test_task(t)
    for template in (contract, impl):
        scaffold_args = dict(template.stages[0].validation_args)
        brief = scaffold_args.get("fallback_brief", "")
        assert "## Key deliverables" in brief
        assert "- add" in brief
        assert "- lookup" in brief
        assert "- list" in brief


def test_task_description_to_brief_edge_cases():
    """Empty or descriptionless tasks produce empty brief (scaffolder then
    falls back to its own single-stub behavior)."""
    from agora.plan.loader import _task_description_to_brief

    assert _task_description_to_brief("") == ""
    assert _task_description_to_brief("write some tests") == ""


def test_instantiate_plan_respects_explicit_stages(tmp_path: Path):
    """A test-ish task that already declares explicit stages is NOT wrapped."""
    plan = Plan(
        flow=Flow(
            name="rt",
            description="",
            agents=(_agent("tester", AgentRole.TESTER),),
            task_graph=(
                _task(
                    id_="tests",
                    assigned_to="tester",
                    output_path="tests/test_x.py",
                    postconditions=(
                        PostconditionRef(name="pytest_passes", args=()),
                    ),
                    stages=(
                        StageTemplate(
                            name="custom_llm",
                            kind="llm",
                            instruction="custom",
                        ),
                    ),
                ),
            ),
        )
    )
    _, _, staged = instantiate_plan(plan, project_name="rt")
    assert "tests" in staged
    assert len(staged["tests"].stages) == 1
    assert staged["tests"].stages[0].name == "custom_llm"


# =================================================================
# v2.9 Phase 2 F4 — fill_assertions stages hide whole-file-write tools
# =================================================================


def test_contract_fill_assertions_hides_write_tools():
    """Contract fill_assertions must force fill_test_body path by hiding
    write_file / edit_file_* / delete_file. Otherwise the tester can
    bypass the return-type drift gate via a raw rewrite."""
    plan = Plan(
        flow=Flow(
            name="p",
            description="",
            agents=(_agent(),),
            task_graph=(
                _task(
                    id_="tests",
                    output_path="tests/test_x.py",
                    postconditions=(
                        PostconditionRef(
                            name="file_exists",
                            args=(("rel", "tests/test_x.py"),),
                        ),
                        PostconditionRef(
                            name="pytest_passes", args=(("rel", "tests/"),)
                        ),
                    ),
                ),
            ),
        )
    )
    _, _, staged = instantiate_plan(plan, project_name="x")
    # Contract task was injected by the loader.
    contract = staged["tests_contract"]
    fill = next(s for s in contract.stages if s.name == "fill_assertions")
    for forbidden in (
        "write_file",
        "delete_file",
        "edit_file_replace",
        "edit_file_insert_before",
        "edit_file_append",
    ):
        assert forbidden in fill.hide_tools, (
            f"expected {forbidden} in hide_tools, got {fill.hide_tools}"
        )


def test_impl_fill_assertions_hides_write_tools():
    """Same for the impl-test fill_assertions stage."""
    plan = Plan(
        flow=Flow(
            name="p",
            description="",
            agents=(_agent(),),
            task_graph=(
                _task(
                    id_="tests",
                    output_path="tests/test_x.py",
                    postconditions=(
                        PostconditionRef(
                            name="file_exists",
                            args=(("rel", "tests/test_x.py"),),
                        ),
                        PostconditionRef(
                            name="pytest_passes", args=(("rel", "tests/"),)
                        ),
                    ),
                ),
            ),
        )
    )
    _, _, staged = instantiate_plan(plan, project_name="x")
    impl = staged["tests"]  # the impl-test half after split
    fill = next(s for s in impl.stages if s.name == "fill_assertions")
    assert "write_file" in fill.hide_tools
    assert "edit_file_replace" in fill.hide_tools


# =================================================================
# v2.9 Phase 3 F3 — class_attributes_consistent auto-injected on impl tasks
# =================================================================


def test_impl_task_gets_class_attrs_consistency_auto_injected():
    """An impl task with py_compiles(src/*.py) receives
    class_attributes_consistent unconditionally — architect-omission
    is the dominant observed mode."""
    from agora.core.agent import AgentConfig

    plan = Plan(
        flow=Flow(
            name="p",
            description="",
            api_spec="## module: src/core.py\n\nclass X:\n    def run(self) -> None: ...\n",
            agents=(
                AgentConfig(name="impl", role=AgentRole.IMPLEMENTER),
            ),
            task_graph=(
                TaskTemplate(
                    id="build",
                    assigned_to="impl",
                    description="b",
                    depends_on=(),
                    output_path="src/core.py",
                    postconditions=(
                        PostconditionRef(
                            name="file_exists", args=(("rel", "src/core.py"),)
                        ),
                        PostconditionRef(
                            name="py_compiles", args=(("rel", "src/core.py"),)
                        ),
                        PostconditionRef(name="mark_complete", args=()),
                    ),
                ),
            ),
        )
    )
    _, tasks, _ = instantiate_plan(plan, project_name="x")
    build = next(t for t in tasks if t.id == "build")
    pc_names = [p.name for p in build.spec.postconditions]
    # The registered name for class_attributes_consistent is derived
    # from the rel — match the pattern.
    assert any(
        "attrs_consistent" in n for n in pc_names
    ), f"expected class_attributes_consistent to be auto-injected, got {pc_names}"


def test_class_attrs_not_injected_on_non_src_py_task():
    """Tasks that py_compile NON src/*.py files (unusual; e.g.
    requirements.txt tasks wrongly using py_compiles) don't get the
    attrs predicate — the check is only meaningful for src/ code."""
    plan = Plan(
        flow=Flow(
            name="p",
            description="",
            agents=(_agent(role=AgentRole.IMPLEMENTER, name="impl"),),
            task_graph=(
                TaskTemplate(
                    id="odd",
                    assigned_to="impl",
                    description="odd",
                    depends_on=(),
                    output_path="scripts/run.py",
                    postconditions=(
                        PostconditionRef(
                            name="file_exists",
                            args=(("rel", "scripts/run.py"),),
                        ),
                        PostconditionRef(
                            name="py_compiles",
                            args=(("rel", "scripts/run.py"),),
                        ),
                        PostconditionRef(name="mark_complete", args=()),
                    ),
                ),
            ),
        )
    )
    _, tasks, _ = instantiate_plan(plan, project_name="x")
    odd = next(t for t in tasks if t.id == "odd")
    pc_names = [p.name for p in odd.spec.postconditions]
    # No attrs-consistency injection because the src/*.py filter.
    assert not any("attrs_consistent" in n for n in pc_names), pc_names


def test_class_attrs_not_double_injected():
    """If the architect already attached class_attributes_consistent,
    the auto-injector must not duplicate it."""
    plan = Plan(
        flow=Flow(
            name="p",
            description="",
            agents=(_agent(role=AgentRole.IMPLEMENTER, name="impl"),),
            task_graph=(
                TaskTemplate(
                    id="build",
                    assigned_to="impl",
                    description="b",
                    depends_on=(),
                    output_path="src/x.py",
                    postconditions=(
                        PostconditionRef(
                            name="file_exists", args=(("rel", "src/x.py"),)
                        ),
                        PostconditionRef(
                            name="py_compiles", args=(("rel", "src/x.py"),)
                        ),
                        PostconditionRef(
                            name="class_attributes_consistent",
                            args=(("rel", "src/x.py"),),
                        ),
                        PostconditionRef(name="mark_complete", args=()),
                    ),
                ),
            ),
        )
    )
    _, tasks, _ = instantiate_plan(plan, project_name="x")
    build = next(t for t in tasks if t.id == "build")
    pc_names = [p.name for p in build.spec.postconditions]
    # Exactly one attrs-consistency predicate, not two.
    n = sum(1 for name in pc_names if "attrs_consistent" in name)
    assert n == 1, f"expected 1 attrs_consistent postcondition, got {n}: {pc_names}"
