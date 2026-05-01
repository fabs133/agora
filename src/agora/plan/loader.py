"""Plan loader — ``(agents, tasks, staged_tasks)`` from a v2.0 plan YAML.

This is the ``Flow`` loader layered with staging + predicate-registry plumbing.
It reuses every piece of :mod:`agora.core.flow` (YAML I/O, include resolution,
variable substitution, Pydantic validation) and adds:

- **Preserved string task ids** — needed so a ``staged_tasks: dict[str, StagedTask]``
  keyed by the YAML's ``id`` still matches after instantiation.
- **StagedTask construction** — for every task with ``stages`` in the template,
  build a :class:`~agora.fleet.stage_runner.StagedTask` and key it by the task id.
- **Test-task auto-staging** (v2.4) — tasks that write a file under ``tests/``
  and expect ``pytest_passes`` get three framework stages injected
  (scaffold → llm → run_pytest) so the 7B tester stops guessing imports.

The return triple matches what
:meth:`agora.fleet.orchestrator.Orchestrator.run_project` already consumes, so
no orchestrator changes are required.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from pathlib import Path

from agora.core.agent import AgentConfig
from agora.core.flow import (
    Flow,
    PostconditionRef,
    StageTemplate,
    TaskTemplate,
    instantiate_flow,
    load_flow,
    save_flow,
)
from agora.core.task import Task
from agora.fleet.stage_runner import Stage, StagedTask

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Plan:
    """Thin wrapper over :class:`Flow` exposing the richer plan shape.

    Currently just the Flow plus convenience accessors; kept as a separate type
    so future plan-level metadata (version tags, authorship, cost estimates,
    decision-point lists) can be added without touching the Flow dataclass.
    """

    flow: Flow

    @property
    def name(self) -> str:
        return self.flow.name

    @property
    def description(self) -> str:
        return self.flow.description

    @property
    def agents(self) -> tuple[AgentConfig, ...]:
        return self.flow.agents

    @property
    def task_graph(self):
        return self.flow.task_graph


def load_plan(path: str | Path) -> Plan:
    """Load + validate + resolve includes for a plan YAML, returning a :class:`Plan`."""
    return Plan(flow=load_flow(path))


def save_plan(plan: Plan, path: str | Path) -> None:
    """Serialize a :class:`Plan` back to YAML (auto-picks v1.0 or v2.0)."""
    save_flow(plan.flow, path)


def _task_description_to_brief(description: str) -> str:
    """Synthesize a fallback brief from a test task's description.

    The plan-builder emits test tasks with descriptions like ``"pytest tests
    covering the deliverables from the brief (add/lookup/list plus edge
    cases)"``. We extract the ``add/lookup/list`` list (or any similar
    slash- or comma-separated list) and emit a ``## Key deliverables``
    markdown section with one bullet per item, so the scaffolder can parse
    it the same way it parses plan/brief.md.

    Returns an empty string if no list is found; the scaffolder will then
    emit its single-generic-stub fallback.
    """
    if not description:
        return ""
    import re as _re

    # Look for the most specific pattern first: slash-separated items in
    # parentheses (e.g. "(add/lookup/list plus edge cases)").
    paren_match = _re.search(r"\(([^)]+)\)", description)
    candidates: list[str] = []
    if paren_match:
        body = paren_match.group(1)
        # Split on "/", ",", " and ", " plus ".
        tokens = _re.split(r"\s*(?:/|,| and | plus )\s*", body)
        candidates = [t.strip() for t in tokens if t.strip()]
    if not candidates:
        # Fallback: comma-separated items anywhere in the description.
        # Require MULTIPLE chunks — a single chunk isn't a list.
        chunks = [
            c.strip() for c in _re.split(r"[,;]", description) if c.strip()
        ]
        if len(chunks) >= 2:
            candidates = [c for c in chunks if 2 <= len(c.split()) <= 8]
    if len(candidates) < 2:
        # Still only a single item → not a real list; bail out.
        return ""
    bullets = "\n".join(f"- {c}" for c in candidates)
    return f"## Key deliverables\n{bullets}\n"


def _test_file_paths(t: TaskTemplate) -> list[str]:
    """Return ALL ``tests/test_<x>.py`` paths this task is meant to produce.

    Scans both the task's ``output_path`` and every ``file_exists(rel=...)``
    postcondition for paths starting with ``tests/``. Order of appearance
    preserved (output_path first, then postconditions). Duplicates dropped.

    Plan-builders sometimes emit a single test task with multiple required
    files (e.g. tests/test_add.py, tests/test_lookup.py, tests/test_list.py).
    v2.7 splits such tasks into N parallel impl tasks — one per file — so
    every declared file gets scaffolded + filled. Without this split only
    the first file would be written and the others' file_exists postconditions
    would fail forever.
    """
    paths: list[str] = []
    seen: set[str] = set()

    def _is_test_py(rel: str) -> bool:
        """A real ``tests/*.py`` file path, not a directory or a bare stem."""
        r = rel.replace("\\", "/")
        return r.startswith("tests/") and r.endswith(".py")

    def _normalize(rel: str) -> str:
        """Weak plan-builders sometimes write ``test_add.py`` without the
        ``tests/`` prefix. Auto-prepend so downstream tools resolve correctly."""
        r = rel.replace("\\", "/")
        if r.endswith(".py") and r.startswith("test_") and "/" not in r:
            return f"tests/{r}"
        return r

    op = _normalize(t.output_path) if t.output_path else ""
    if _is_test_py(op) and op not in seen:
        paths.append(op)
        seen.add(op)
    for pc in t.postconditions:
        if pc.name != "file_exists":
            continue
        rel = _normalize(str(pc.args_dict().get("rel", "")))
        if _is_test_py(rel) and rel not in seen:
            paths.append(rel)
            seen.add(rel)
    return paths


def _test_file_path(t: TaskTemplate) -> str:
    """Return the FIRST test file path declared on the task (or empty).

    Kept for back-compat callers that expect a single string; new loader
    logic should use :func:`_test_file_paths` to see the complete list.
    """
    paths = _test_file_paths(t)
    return paths[0] if paths else ""


def _is_test_authoring_task(t: TaskTemplate) -> bool:
    """A task that writes a test file and expects pytest to pass is a
    candidate for the scaffold-+-verify wrapper. Tasks that already define
    explicit stages are respected as-is (the transform is opt-in by
    virtue of the plan NOT specifying stages)."""
    if t.stages:
        return False
    if not _test_file_path(t):
        return False
    return any(pc.name == "pytest_passes" for pc in t.postconditions)


#: Where contract tests live (framework-owned path, identical across plans).
_CONTRACT_TEST_PATH = "tests/test_contract.py"
#: Where per-test intent prose lives — written by the derive_intent stage,
#: read by fill_assertions as context. Separate files per mode so contract
#: and impl don't overwrite each other.
_CONTRACT_INTENT_PATH = "plan/kb/test_intent_contract.md"
_IMPL_INTENT_PATH = "plan/kb/test_intent_impl.md"


def _contract_llm_instruction(test_path: str) -> str:
    """Prompt text for the contract-tests LLM stage. The tester sees ONLY
    the brief + the scaffolded file — NOT the implementation (which doesn't
    exist yet). Instructs the model to keep imports deferred inside each
    test body so the file can load before any implementation module does.

    CRITICAL prompt-engineering note: do NOT use placeholder tokens like
    ``<pkg>`` or ``<module>`` in example shapes — weak models copy them
    verbatim into the file, breaking Python syntax. Use plausible concrete
    names the model can either reuse OR recognize as examples.
    """
    return (
        "You are authoring CONTRACT tests — behavioral invariants derived "
        "from the project brief. The implementation does not exist yet.\n\n"
        f"The file at {test_path} already contains a set of test functions. "
        "Each function body is a single `pytest.skip(...)` placeholder call. "
        "Your job: replace each placeholder call with real assertions.\n\n"
        f"Read {_CONTRACT_INTENT_PATH} FIRST. It contains one `## test_<name>` "
        "section per test function, written by a prior intent-derivation "
        "stage. Each section describes in prose what the test should verify "
        "— use this as the ground truth for the assertions, not your own "
        "inference from the function name.\n\n"
        "Constraints on what you write:\n"
        "  - Prefer the fill_test_body tool: call `fill_test_body(test_name, "
        "body_code)` once per test. The framework handles file path, "
        "indentation, and docstring preservation — you only supply the "
        "test's python body (deferred import + asserts). No whitespace "
        "matching, no partial edits.\n"
        "  - The function signatures and docstrings already exist above "
        "each placeholder. Do not recreate them — supply just the body.\n"
        "  - Imports of the implementation module must live inside the "
        "function body, not at module scope (the implementation doesn't "
        "exist yet; module-level imports would break pytest collection).\n"
        "  - Write invariant assertions only: type/length checks, "
        "round-trip equality, presence/absence. No implementation-specific "
        "values (hash of a particular input).\n"
        "  - The package name comes from the project. Read src/ and "
        "src/__init__.py if they exist to confirm; otherwise infer "
        "snake_case from the project name.\n\n"
        "After each call, the file must parse as valid Python AND each "
        "test function must contain at least one `assert` statement. A "
        "function left with only pytest.skip is a failed test and will "
        "fail the gates. Stop calling tools when every stub is replaced."
    )


def _impl_llm_instruction(test_path: str, intent_path: str = _IMPL_INTENT_PATH) -> str:
    """Prompt text for the impl-tests LLM stage. Code exists now; tester
    can read it and assert on concrete observable behavior.

    ``intent_path`` is the per-file intent markdown the derive_intent stage
    wrote for THIS test file — pass it explicitly so multi-file impl tasks
    each reference their own intent file, not the shared default.
    """
    return (
        "You are authoring IMPLEMENTATION tests — concrete-value assertions "
        "that complement the contract tests already in tests/test_contract.py.\n\n"
        f"The file at {test_path} already contains module-level imports of "
        "the real implementation modules and a set of test functions whose "
        "bodies are `pytest.skip(...)` placeholder calls. Your job: replace "
        "each placeholder call with real assertions against the "
        "implementation.\n\n"
        f"Read {intent_path} FIRST. It contains one `## test_<name>` "
        "section per test function, written by a prior intent-derivation "
        "stage. Each section describes in prose what the test should verify "
        "— use this as the ground truth for the assertions, not your own "
        "inference from the function name.\n\n"
        "Constraints on what you write:\n"
        "  - Read the source files under src/ to see the real API "
        "shape — you need concrete inputs and outputs, not guesses.\n"
        "  - Prefer the fill_test_body tool: call `fill_test_body(test_name, "
        "body_code)` once per test. The framework handles file path, "
        "indentation, and docstring preservation — you only supply the "
        "test's python body (import + asserts). No whitespace matching, "
        "no partial edits.\n"
        "  - The function signatures and docstrings already exist above "
        "each placeholder. Do not recreate them — supply just the body.\n"
        "  - Module-level imports are already in the file; reuse them.\n"
        "  - Write concrete observations: specific input→output pairs, "
        "edge cases, error paths. Behavioral invariants are already "
        "covered in tests/test_contract.py — don't duplicate them.\n\n"
        "After each call, the file must parse as valid Python AND each "
        "test function must contain at least one `assert` statement. A "
        "function left with only pytest.skip is a failed test and will "
        "fail the gates. Stop calling tools when every stub is replaced."
    )


def _split_test_task(
    t: TaskTemplate, flow_brief: str = ""
) -> list[TaskTemplate]:
    """Split a single test-authoring task into contract + impl variants.

    Contract tests run BEFORE implementers (depend only on setup-class deps);
    impl tests run AFTER (keep the original deps + the new contract task id).
    Implementer tasks are annotated separately by
    :func:`_annotate_code_tasks_with_contract_gate` so they can't complete
    without satisfying the contract tests.

    ``flow_brief`` — v2.6 — the rich brief text embedded in the plan YAML.
    When set, it's passed as the scaffolder's ``fallback_brief`` so the
    tester sees the full deliverable bullets (e.g. "Add a long URL and get
    a short 6-char hash back") rather than the abbreviated
    description-derived bullets (e.g. just "add"). Without this the
    scaffold's per-deliverable stubs lose semantic context and the tester
    LLM has to guess what each test name means.
    """
    # v2.7: gather ALL test file paths from the task (output_path + every
    # file_exists postcondition). Plans sometimes declare 3+ test files under
    # one task; we emit one impl task per file so each gets scaffolded +
    # filled. Fall back to a single synthetic path if nothing's declared.
    impl_paths = _test_file_paths(t)
    if not impl_paths:
        impl_paths = [t.output_path or "tests/test_impl.py"]
    # Prefer the flow-level brief (rich, from plan-builder) over the
    # description-derived fallback (abbreviated).
    fallback_brief = flow_brief.strip() or _task_description_to_brief(t.description)

    # Contract task — runs early; scaffold(mode=contract) + llm(no src access)
    # + NO pytest verify (implementation doesn't exist yet, verification is
    # deferred to the annotated implementer tasks).
    #
    # BUT we DO gate on ``py_compiles`` so the tester's output must at least
    # parse as valid Python. Observed failure (Sprint 7 run 1): 7B copied a
    # placeholder token ``<pkg>.<mod>`` verbatim into the test body, producing
    # a SyntaxError that pytest couldn't collect. Without the py_compiles
    # gate, the broken contract file flowed through to implementers whose
    # ``pytest_passes(tests/test_contract.py)`` postcondition then failed
    # with an obscure collection error. Failing it at the contract boundary
    # surfaces the real problem + retries the tester, not the implementer.
    contract_id = f"{t.id}_contract"
    contract = TaskTemplate(
        id=contract_id,
        assigned_to=t.assigned_to,
        description=(
            "Contract tests — authoring behavioral invariants from the brief. "
            "Implementation does not exist yet; use deferred imports inside "
            "each test body."
        ),
        depends_on=(),  # set to [] — annotator will point implementers here
        output_path=_CONTRACT_TEST_PATH,
        postconditions=(
            PostconditionRef(
                name="file_exists",
                args=(("rel", _CONTRACT_TEST_PATH),),
            ),
            PostconditionRef(
                name="py_compiles",
                args=(("rel", _CONTRACT_TEST_PATH),),
            ),
            # Sprint 7.1: force real assertions, not pytest.skip stubs.
            PostconditionRef(
                name="tests_have_assertions",
                args=(("rel", _CONTRACT_TEST_PATH),),
            ),
            PostconditionRef(name="mark_complete", args=()),
        ),
        stages=(
            StageTemplate(
                name="scaffold",
                kind="plan_scaffold_tests",
                output_path=_CONTRACT_TEST_PATH,
                validation_args=(
                    ("fallback_brief", fallback_brief),
                    ("mode", "contract"),
                ),
            ),
            # v2.6 — per-deliverable intent derivation. One narrow LLM call
            # per test function, writes prose intent to plan/kb/test_intent.md.
            # The next stage reads this intent file as context so it doesn't
            # have to infer meaning AND write code in a single turn.
            StageTemplate(
                name="derive_intent",
                kind="plan_derive_test_intent",
                output_path=_CONTRACT_INTENT_PATH,
                validation_args=(
                    ("scaffold_path", _CONTRACT_TEST_PATH),
                    ("fallback_brief", fallback_brief),
                    ("mode", "contract"),
                ),
            ),
            StageTemplate(
                name="fill_assertions",
                kind="llm",
                instruction=_contract_llm_instruction(_CONTRACT_TEST_PATH),
                context_files=(
                    "plan/brief.md",
                    _CONTRACT_INTENT_PATH,
                    _CONTRACT_TEST_PATH,
                ),
                max_iterations=10,
                # v2.9 Phase 2 gate lives inside fill_test_body. Hide the
                # whole-file-write tools so the tester MUST use
                # fill_test_body per assertion — bypassing via write_file
                # would skip the return-type drift check. delete_file +
                # edit_* removed for the same reason: don't let the model
                # hand-edit the test file outside the structural-check
                # path.
                hide_tools=(
                    "write_file",
                    "delete_file",
                    "edit_file_replace",
                    "edit_file_insert_before",
                    "edit_file_append",
                ),
            ),
        ),
    )

    # Impl tasks — one per declared test file. Each runs AFTER implementers
    # (depends on contract + original deps). Each owns its own file, intent
    # path, and verify scope. Siblings run in parallel (only depend on
    # contract + original deps, not on each other).
    #
    # Sprint 7.1: inject tests_have_assertions(rel=impl_path) so the impl
    # tester can't leave pytest.skip stubs behind.
    # Sprint 7.7: each impl owns pytest_passes(rel=this_file) scoped to its
    # own file rather than the broader tests/ dir — lets each task fail/retry
    # independently without waiting on sibling completion.
    impl_deps = tuple(list(t.depends_on) + [contract_id])
    # Non-file-specific postconditions from the original task (mark_complete,
    # anything non-tests/* file_exists, custom predicates) carry through to
    # every impl task. File-specific postconditions (file_exists/py_compiles/
    # pytest_passes on tests/*.py and the broad pytest_passes(tests/)) are
    # REPLACED per-file.
    def _is_file_specific(pc: PostconditionRef) -> bool:
        rel = str(pc.args_dict().get("rel", ""))
        if pc.name in ("file_exists", "py_compiles", "tests_have_assertions"):
            return rel.startswith("tests/")
        if pc.name == "pytest_passes":
            # Drop both the per-file and the broad tests/ form; we re-emit
            # per-file below.
            return True
        return False

    carryover_pcs = tuple(
        pc for pc in t.postconditions if not _is_file_specific(pc)
    )

    impl_tasks: list[TaskTemplate] = []
    multiple = len(impl_paths) > 1
    for idx, impl_path in enumerate(impl_paths):
        # Single-file case: keep original task id for back-compat.
        # Multi-file case: suffix with stem so each task has a distinct id.
        if multiple:
            stem = impl_path.removeprefix("tests/").removesuffix(".py")
            # Sanitize to snake_case.
            stem = stem.replace("/", "_").replace("-", "_")
            task_id = f"{t.id}_{stem}"
        else:
            task_id = t.id

        # Per-file intent path — multi-file case needs distinct files so
        # siblings don't overwrite each other's intent.
        if multiple:
            intent_stem = impl_path.removeprefix("tests/").removesuffix(".py")
            intent_path = f"plan/kb/test_intent_{intent_stem}.md"
        else:
            intent_path = _IMPL_INTENT_PATH

        impl_pcs = carryover_pcs + (
            PostconditionRef(
                name="file_exists",
                args=(("rel", impl_path),),
            ),
            PostconditionRef(
                name="tests_have_assertions",
                args=(("rel", impl_path),),
            ),
            PostconditionRef(
                name="pytest_passes",
                args=(("rel", impl_path),),
            ),
        )

        impl_tasks.append(
            TaskTemplate(
                id=task_id,
                assigned_to=t.assigned_to,
                description=t.description,
                depends_on=impl_deps,
                output_path=impl_path,
                precondition_descriptions=t.precondition_descriptions,
                postcondition_descriptions=t.postcondition_descriptions,
                postconditions=impl_pcs,
                stages=(
                    StageTemplate(
                        name="scaffold",
                        kind="plan_scaffold_tests",
                        output_path=impl_path,
                        validation_args=(
                            ("fallback_brief", fallback_brief),
                            ("mode", "impl"),
                        ),
                    ),
                    StageTemplate(
                        name="derive_intent",
                        kind="plan_derive_test_intent",
                        output_path=intent_path,
                        validation_args=(
                            ("scaffold_path", impl_path),
                            ("fallback_brief", fallback_brief),
                            ("mode", "impl"),
                        ),
                    ),
                    StageTemplate(
                        name="fill_assertions",
                        kind="llm",
                        instruction=_impl_llm_instruction(impl_path, intent_path),
                        context_files=(
                            "plan/brief.md",
                            intent_path,
                            impl_path,
                        ),
                        max_iterations=10,
                        # v2.9 Phase 2 (F4 fix): force fill_test_body path
                        # so the return-type drift gate can't be bypassed
                        # via whole-file rewrites.
                        hide_tools=(
                            "write_file",
                            "delete_file",
                            "edit_file_replace",
                            "edit_file_insert_before",
                            "edit_file_append",
                        ),
                    ),
                    StageTemplate(
                        name="verify",
                        kind="plan_run_pytest",
                        output_path=f"plan/kb/pytest_output_{idx}.md"
                        if multiple
                        else "plan/kb/pytest_output.md",
                    ),
                ),
            )
        )

    logger.info(
        "loader: splitting test task %r → %r (contract) + %d impl task(s) %r",
        t.id, contract.id, len(impl_tasks), [it.id for it in impl_tasks],
    )
    return [contract, *impl_tasks]


def _align_impl_tasks_to_spec(
    templates: list[TaskTemplate], api_spec_text: str
) -> list[TaskTemplate]:
    """Remap implementer tasks so each targets a module from the api_spec.

    An "implementer task" here is any template whose postconditions include
    ``py_compiles(rel=src/*.py)`` — i.e. the plan-builder marked it as the
    owner of a src/ file.

    Rules:
      - If the task's current src path matches a spec module → leave it.
      - Else if there's an unused spec module → remap output_path +
        matching postcondition rels to that spec module.
      - Else (more impl tasks than spec modules) → **raise** :class:`AgoraError`
        with a structured message naming the offending task(s) and known
        spec modules. v2.8 (Approach C — C4b): the prior behaviour silently
        dropped the task and its dependents, which hid mistakes from the
        architect and shipped incoherent plans (observed in live runs: a
        ``cli_entry_point`` task disappearing because the architect forgot
        to list ``src/cli.py`` in api_spec). Loud failure lets the
        ``finalize_plan`` framework stage surface the error as an ERROR
        return so the in-phase retry fires on ``author_tasks`` with the
        structured message injected as a learning.

    Returns the aligned template list. Skips the transform when
    ``api_spec_text`` is empty (Sprint 7.2-7.4 plans without a spec).
    """
    from agora.core.errors import AgoraError
    from agora.plan.api_spec import parse_api_spec

    if not api_spec_text:
        return templates
    all_spec_modules = [m.path for m in parse_api_spec(api_spec_text)]
    # v2.7(c): only PRODUCTION src/*.py modules are valid implementer targets.
    # A spec that accidentally includes ``src/tests/test_X.py`` (observed on
    # 7B) must not cause the align logic to remap implementer output paths
    # into test-file paths.
    def _is_production(path: str) -> bool:
        norm = path.replace("\\", "/")
        if norm.startswith("tests/") or norm.startswith("src/tests/"):
            return False
        return norm.startswith("src/") and norm.endswith(".py")

    spec_modules = [m for m in all_spec_modules if _is_production(m)]
    if not spec_modules:
        return templates
    spec_set = set(spec_modules)

    def _impl_src_path(t: TaskTemplate) -> str:
        """Return the src/*.py rel the task targets, or empty string."""
        if t.output_path.startswith("src/") and t.output_path.endswith(".py"):
            return t.output_path
        for pc in t.postconditions:
            if pc.name == "py_compiles":
                rel = str(pc.args_dict().get("rel", ""))
                if rel.startswith("src/") and rel.endswith(".py"):
                    return rel
        return ""

    # First pass: identify implementer tasks and which are already aligned.
    impl_indices: list[int] = []
    already_used: set[str] = set()
    for i, t in enumerate(templates):
        src_path = _impl_src_path(t)
        if not src_path:
            continue
        impl_indices.append(i)
        if src_path in spec_set:
            already_used.add(src_path)

    unused_spec = [m for m in spec_modules if m not in already_used]
    out: list[TaskTemplate] = list(templates)
    # v2.8(C4b): collect tasks that would otherwise have been dropped so we
    # can raise with a complete list instead of failing on the first one.
    would_drop: list[tuple[str, str]] = []  # (task_id, src_path)
    for i in impl_indices:
        t = out[i]
        src_path = _impl_src_path(t)
        if src_path in spec_set:
            continue  # already aligned
        if unused_spec:
            new_path = unused_spec.pop(0)
            new_pcs = tuple(
                _remap_src_rel(pc, src_path, new_path) for pc in t.postconditions
            )
            new_output = new_path if t.output_path == src_path else t.output_path
            if new_output == "" and t.output_path == "":
                new_output = new_path  # promote
            # If the task's own output_path was the old src path, update it.
            if t.output_path == src_path or not t.output_path:
                new_output = new_path
            else:
                new_output = t.output_path
            logger.info(
                "loader: remapping impl task %r from %r → %r (api_spec alignment)",
                t.id, src_path, new_path,
            )
            out[i] = dataclasses.replace(
                t, output_path=new_output, postconditions=new_pcs
            )
        else:
            would_drop.append((t.id, src_path))

    if would_drop:
        lines = [
            f"plan has {len(would_drop)} implementer task(s) targeting "
            "src/*.py module(s) not in api_spec, and no unclaimed spec "
            "modules left to remap them to:",
        ]
        for tid, src_path in would_drop:
            lines.append(f"  - task {tid!r} -> {src_path!r}")
        lines.append(
            f"known api_spec modules: {sorted(spec_set)}"
        )
        lines.append(
            "fix: re-author the plan so every implementer task's output "
            "module is declared in plan/api_spec.md. Either add the "
            "missing module(s) to api_spec, or remove/retarget the "
            "offending task(s)."
        )
        raise AgoraError("\n".join(lines))
    return out


def _remap_src_rel(
    pc: PostconditionRef, old_rel: str, new_rel: str
) -> PostconditionRef:
    """If ``pc`` references ``old_rel`` via its ``rel`` arg, rewrite to
    ``new_rel``. Otherwise return unchanged."""
    if not old_rel:
        return pc
    args = dict(pc.args)
    if args.get("rel") == old_rel:
        args["rel"] = new_rel
        return PostconditionRef(name=pc.name, args=tuple(sorted(args.items())))
    return pc


def _auto_inject_class_attrs_consistency(
    tasks: list[TaskTemplate],
) -> list[TaskTemplate]:
    """v2.9 Phase 3 follow-up: for every impl task that py_compiles a
    src/*.py file, inject ``class_attributes_consistent(rel=...)`` as a
    postcondition unless the architect already attached it.

    Catches the 2026-04-22 ``self.url_mapping`` vs ``self.url_hash_map``
    typo at write time. Architect-omission of this predicate is the
    dominant failure mode observed — auto-injection is unconditional
    because the check is permissive-leaning (only flags reads with no
    matching set, and that's almost always a real typo).
    """
    updated: list[TaskTemplate] = []
    for t in tasks:
        # Find the src/*.py target (if any) for this task.
        src_rel: str | None = None
        for pc in t.postconditions:
            if pc.name == "py_compiles":
                rel = str(pc.args_dict().get("rel", ""))
                if rel.startswith("src/") and rel.endswith(".py"):
                    src_rel = rel
                    break
        if src_rel is None:
            updated.append(t)
            continue
        # Skip if already attached.
        already = any(
            pc.name == "class_attributes_consistent"
            and dict(pc.args).get("rel") == src_rel
            for pc in t.postconditions
        )
        if already:
            updated.append(t)
            continue
        new_pc = PostconditionRef(
            name="class_attributes_consistent",
            args=(("rel", src_rel),),
        )
        logger.info(
            "loader: auto-injecting class_attributes_consistent(rel=%r) "
            "on task %r",
            src_rel, t.id,
        )
        updated.append(
            dataclasses.replace(
                t, postconditions=t.postconditions + (new_pc,)
            )
        )
    return updated


def _annotate_code_tasks_with_contract_gate(
    tasks: list[TaskTemplate], contract_id: str
) -> list[TaskTemplate]:
    """For every task carrying a ``py_compiles`` postcondition, add a
    dependency on ``contract_id`` + a ``pytest_passes(tests/test_contract.py)``
    postcondition. The implementer can't finish without satisfying the
    contract tests authored earlier.
    """
    updated: list[TaskTemplate] = []
    for t in tasks:
        # Skip the contract/impl tasks themselves — they don't gate each other.
        if t.id == contract_id:
            updated.append(t)
            continue
        has_py_compiles = any(
            pc.name == "py_compiles" for pc in t.postconditions
        )
        if not has_py_compiles:
            updated.append(t)
            continue
        new_deps = tuple(t.depends_on)
        if contract_id not in new_deps:
            new_deps = new_deps + (contract_id,)
        # Add pytest_passes on tests/test_contract.py if not already present.
        gate_pc = PostconditionRef(
            name="pytest_passes",
            args=(("rel", _CONTRACT_TEST_PATH),),
        )
        already_has_gate = any(
            pc.name == "pytest_passes"
            and dict(pc.args).get("rel") == _CONTRACT_TEST_PATH
            for pc in t.postconditions
        )
        new_pcs = t.postconditions
        if not already_has_gate:
            new_pcs = new_pcs + (gate_pc,)
        logger.info(
            "loader: annotating task %r with depends_on += [%s] + pytest_passes gate",
            t.id, contract_id,
        )
        updated.append(
            dataclasses.replace(t, depends_on=new_deps, postconditions=new_pcs)
        )
    return updated


def instantiate_plan(
    plan: Plan,
    project_name: str,
    variables: dict[str, str] | None = None,
) -> tuple[list[AgentConfig], list[Task], dict[str, StagedTask]]:
    """Build the triple ``Orchestrator.run_project`` consumes.

    String task ids are preserved (unlike :func:`instantiate_flow` default) so
    the ``staged_tasks`` dict keys match the ``task.id`` values downstream.
    Variable substitution (``${project_name}`` etc.) applies to task descriptions
    and agent instructions just like a v1.0 flow.

    v2.4: transparently wraps test-authoring tasks with a scaffold → llm →
    verify 3-stage pipeline so the 7B tester stops guessing imports.
    """

    # Apply the test-task transform on the template list BEFORE passing into
    # instantiate_flow. v2.4 wrapped a single test task with scaffold/llm/verify;
    # v2.5 SPLITS it into contract+impl (contract runs early, impl runs after
    # implementers). Idempotent — tasks with explicit stages are skipped.
    split_templates: list[TaskTemplate] = []
    contract_ids: list[str] = []
    for t in plan.flow.task_graph:
        if _is_test_authoring_task(t):
            pair = _split_test_task(t, flow_brief=plan.flow.brief)
            split_templates.extend(pair)
            # The contract task is the first item; record its id so the
            # code-task annotator can point implementers at it.
            contract_ids.append(pair[0].id)
        else:
            # v2.7: plans occasionally emit implementer tasks with no
            # explicit output_path, relying on a file_exists postcondition
            # to specify the file. The model then has to infer the path,
            # which 7B gets wrong (observed: wrote 'src_core_domain.py'
            # instead of 'src/core_domain.py'). Fix: if output_path is
            # empty but there's exactly one file_exists postcondition,
            # promote that rel as the output_path so the banner tells the
            # model where to write.
            if not t.output_path:
                file_exists_rels = [
                    str(pc.args_dict().get("rel", ""))
                    for pc in t.postconditions
                    if pc.name == "file_exists"
                ]
                file_exists_rels = [r for r in file_exists_rels if r]
                if len(file_exists_rels) == 1:
                    t = dataclasses.replace(t, output_path=file_exists_rels[0])
                    logger.info(
                        "loader: promoting file_exists rel to output_path "
                        "for task %r: %r",
                        t.id, file_exists_rels[0],
                    )
            split_templates.append(t)

    # v2.7 Sprint 7.5 fix: remap implementer tasks to match the api_spec.
    # Observed failure: plan-builder's ``define_api`` spec's one module
    # (src/cli.py) but ``author_tasks`` emitted two implementer tasks
    # (add_core_domain_module → src/domain.py AND add_cli_entry_point →
    # src/cli.py). The "extra" task targets an unstubbed file, breaks the
    # contract-tests-vs-implementation coordination we paid for with the
    # shared spec, and the ONE matching implementer (cli) bails because
    # the stub already exists and it doesn't know it should FILL it.
    #
    # Fix: walk the tasks; for each implementer task (has py_compiles on
    # src/*.py), remap its output_path + postconditions to a module in
    # the spec. Extra tasks get dropped (with dependent tasks rewired).
    split_templates = _align_impl_tasks_to_spec(split_templates, plan.flow.api_spec)

    # For each contract task we created, annotate all py_compiles-carrying
    # tasks with a dep + pytest_passes gate so the implementer can't finish
    # without honoring the contract.
    annotated = split_templates
    for contract_id in contract_ids:
        annotated = _annotate_code_tasks_with_contract_gate(annotated, contract_id)

    # v2.9 Phase 3: auto-inject class_attributes_consistent on every impl
    # task. Closes the F3 gap where the architect forgets to attach it
    # (observed: the YAML instruction recommends it, but the LLM doesn't
    # always include it). The check is permissive-leaning — false
    # positives are rare, so unconditional injection is safe.
    annotated = _auto_inject_class_attrs_consistency(annotated)

    wrapped_templates = tuple(annotated)
    wrapped_flow = dataclasses.replace(plan.flow, task_graph=wrapped_templates)

    agents, tasks = instantiate_flow(
        wrapped_flow, project_name, variables, id_strategy="preserve"
    )

    # Map task_id → Task so we can attach StagedTasks by id cheaply below.
    task_by_id = {t.id: t for t in tasks}

    staged_tasks: dict[str, StagedTask] = {}
    for template in wrapped_templates:
        if not template.stages:
            continue
        task = task_by_id.get(template.id)
        if task is None:
            continue
        staged_tasks[template.id] = StagedTask(
            task=task,
            stages=[
                Stage(
                    name=st.name,
                    instruction=st.instruction,
                    context_files=tuple(st.context_files),
                    max_iterations=st.max_iterations,
                    # v2.1: propagate decision-stage fields from the YAML template
                    # through to the runtime Stage so StageRunner can dispatch.
                    kind=st.kind,
                    decision_id=st.decision_id,
                    question=st.question,
                    options=tuple(st.options),
                    output_path=st.output_path,
                    # v2.3: typed validation args for plan_validate_agent etc.
                    validation_args=dict(st.validation_args),
                    # v2.3: per-stage tool-manifest filter.
                    hide_tools=tuple(st.hide_tools),
                )
                for st in template.stages
            ],
        )

    return agents, tasks, staged_tasks


__all__ = ["Plan", "instantiate_plan", "load_plan", "save_plan"]
