"""Reusable flow/package definitions — YAML-declared agent team + task graph templates."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from agora.core.agent import DEFAULT_MODEL, AgentConfig
from agora.core.contract import Specification, make_predicate
from agora.core.errors import AgoraError
from agora.core.task import Task
from agora.core.types import AgentRole, TaskStatus


@dataclass(frozen=True)
class PostconditionRef:
    """Typed postcondition reference resolved at instantiate time via the plan registry.

    v2.0 plans use this in place of ``postcondition_descriptions`` so the
    declarative YAML can hold real postcondition semantics instead of free text.
    """

    name: str
    args: tuple[tuple[str, Any], ...] = ()  # frozen kv pairs so the whole TaskTemplate stays hashable

    def args_dict(self) -> dict[str, Any]:
        return dict(self.args)


@dataclass(frozen=True)
class StageTemplate:
    """Serializable description of a :class:`~agora.fleet.stage_runner.Stage`.

    ``instruction`` is free-form text (rendered as a YAML block scalar) for
    ``kind="llm"`` stages. ``validation`` callables are not supported in plan
    YAML — express them as task-level postconditions instead.

    ``kind="decision"`` stages have no LLM call at all. The framework posts
    ``question`` + ``options`` as a Matrix question-card, awaits the user's
    answer via any of three surfaces (poll click, emoji reaction, or
    ``/agora decision <answer>`` chat command), and writes the chosen answer
    id to ``output_path`` under the task's work_dir. Decision stages take
    ownership of the decision metadata so the LLM cannot paraphrase it — fixes
    the 7B deviation observed in live plan-builder runs.
    """

    name: str
    instruction: str = ""
    context_files: tuple[str, ...] = ()
    max_iterations: int = 5
    # v2.1 additions — decision-stage fields (ignored when kind="llm")
    kind: str = "llm"
    decision_id: str = ""
    question: str = ""
    options: tuple[str, ...] = ()
    output_path: str = ""
    # v2.3 addition — generic kv args for framework stages that need typed
    # inputs (e.g. plan_validate_agent needs expected_name + expected_role).
    # Frozen so the whole StageTemplate stays hashable.
    validation_args: tuple[tuple[str, str], ...] = ()

    def validation_args_dict(self) -> dict[str, str]:
        return dict(self.validation_args)

    # v2.3 addition — per-stage tool scoping. Names listed here are filtered
    # OUT of the LLM's tool manifest for THIS stage only. Used so tasks like
    # ``gather_context`` (which only needs write_file) don't see the plan_*
    # tools and get distracted into calling them mid-stream. Applies only to
    # ``kind="llm"`` stages; ignored on framework/decision stages.
    hide_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskTemplate:
    id: str
    assigned_to: str
    description: str
    depends_on: tuple[str, ...] = ()
    precondition_descriptions: tuple[str, ...] = ()
    postcondition_descriptions: tuple[str, ...] = ()
    # v2.0 additions (all optional; absent → v1.0 semantics)
    postconditions: tuple[PostconditionRef, ...] = ()
    output_path: str = ""
    stages: tuple[StageTemplate, ...] = ()


@dataclass(frozen=True)
class Flow:
    name: str
    description: str
    agents: tuple[AgentConfig, ...]
    task_graph: tuple[TaskTemplate, ...]
    # v2.6 — free-form brief text (markdown). When a plan-builder emits a
    # plan it embeds the rich ``plan/brief.md`` here so that downstream
    # executors (which start in a fresh work_dir with no plan/brief.md)
    # can still ground scaffolder + tester prompts in the real deliverables.
    # Empty string means "no brief"; loader falls back to task-description
    # heuristics in that case.
    brief: str = ""
    # v2.7 — shared API spec (markdown). Authored ONCE by the plan-builder's
    # ``define_api`` task and embedded into the emitted plan.yaml. Propagated
    # to the executor workspace so the contract test scaffolder AND the
    # implementer stub scaffolder both read the SAME source of truth for
    # class names, function names, and signatures. Kills the "tester imagines
    # one API, implementer writes a different one" coordination failure
    # observed on 7B in Sprint 7.1-7.4 live runs.
    api_spec: str = ""


# ------------------------ Pydantic schemas for YAML validation ------------------------


class _AgentSchema(BaseModel):
    name: str
    role: AgentRole
    model: str = DEFAULT_MODEL
    instructions: str = ""
    knowledge_files: list[str] = Field(default_factory=list)


class _PostconditionSchema(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class _StageSchema(BaseModel):
    name: str
    instruction: str = ""
    context_files: list[str] = Field(default_factory=list)
    max_iterations: int = 5
    # v2.1 — decision-stage fields.
    kind: str = "llm"
    decision_id: str = ""
    question: str = ""
    options: list[str] = Field(default_factory=list)
    output_path: str = ""
    # v2.3 — typed arguments for framework validation stages (string values only
    # so the YAML stays human-readable; richer types would require a schema).
    validation_args: dict[str, str] = Field(default_factory=dict)
    # v2.3 — per-stage tool-manifest filter (LLM stages only).
    hide_tools: list[str] = Field(default_factory=list)


class _TaskSchema(BaseModel):
    id: str
    assigned_to: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    precondition_descriptions: list[str] = Field(default_factory=list)
    postcondition_descriptions: list[str] = Field(default_factory=list)
    # v2.0 additions — all optional so v1.0 YAML validates unchanged.
    postconditions: list[_PostconditionSchema] = Field(default_factory=list)
    output_path: str = ""
    stages: list[_StageSchema] = Field(default_factory=list)


class _FlowSchema(BaseModel):
    version: str = "1.0"
    name: str
    description: str = ""
    # v2.6 — rich brief travels alongside the plan so executor workspaces
    # can ground scaffolders + LLM stages in the original deliverables.
    brief: str = ""
    # v2.7 — shared API spec authored once, scaffolders use it to emit
    # matching imports (tests) and stubs (src/).
    api_spec: str = ""
    includes: list[str] = Field(default_factory=list)
    agents: list[_AgentSchema] = Field(default_factory=list)
    task_graph: list[_TaskSchema] = Field(default_factory=list)


SUPPORTED_FLOW_VERSIONS = frozenset({"1.0", "2.0"})


# --------------------------------- Public API ---------------------------------


def load_flow(path: str | Path) -> Flow:
    """Load, validate, and resolve includes for a flow YAML."""
    return _load_flow_recursive(Path(path).resolve(), visiting=set())


def _load_flow_recursive(path: Path, visiting: set[str]) -> Flow:
    key = str(path)
    if key in visiting:
        raise AgoraError(f"flow include cycle detected at {path}")
    visiting = visiting | {key}

    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AgoraError(f"failed to read flow YAML at {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise AgoraError(f"flow YAML at {path} must be a mapping")

    try:
        schema = _FlowSchema.model_validate(raw)
    except ValidationError as exc:
        raise AgoraError(f"invalid flow schema: {exc}") from exc

    if schema.version not in SUPPORTED_FLOW_VERSIONS:
        raise AgoraError(
            f"unsupported flow version {schema.version!r}; "
            f"supported: {sorted(SUPPORTED_FLOW_VERSIONS)}"
        )

    if not schema.agents and not schema.includes:
        raise AgoraError(
            f"flow {schema.name!r} must declare at least one agent or include another flow"
        )

    included_agents: list[AgentConfig] = []
    included_tasks: list[TaskTemplate] = []
    for include_ref in schema.includes:
        ref_path = (path.parent / include_ref).resolve()
        sub_flow = _load_flow_recursive(ref_path, visiting)
        included_agents.extend(sub_flow.agents)
        # Namespace included task ids to avoid collisions with the parent flow.
        prefix = f"{sub_flow.name}:"
        for t in sub_flow.task_graph:
            included_tasks.append(
                TaskTemplate(
                    id=prefix + t.id,
                    assigned_to=t.assigned_to,
                    description=t.description,
                    depends_on=tuple(prefix + d for d in t.depends_on),
                    precondition_descriptions=t.precondition_descriptions,
                    postcondition_descriptions=t.postcondition_descriptions,
                    postconditions=t.postconditions,
                    output_path=t.output_path,
                    stages=t.stages,
                )
            )

    own_agents = [
        AgentConfig(
            name=a.name,
            role=a.role,
            model=a.model,
            instructions=a.instructions,
            knowledge_files=tuple(a.knowledge_files),
        )
        for a in schema.agents
    ]
    agents_list = _dedupe_agents(included_agents + own_agents)
    agent_names = {a.name for a in agents_list}

    own_tasks: list[TaskTemplate] = []
    for t in schema.task_graph:
        if t.assigned_to not in agent_names:
            raise AgoraError(
                f"task {t.id} assigned_to '{t.assigned_to}' does not match any agent"
            )
        own_tasks.append(
            TaskTemplate(
                id=t.id,
                assigned_to=t.assigned_to,
                description=t.description,
                depends_on=tuple(t.depends_on),
                precondition_descriptions=tuple(t.precondition_descriptions),
                postcondition_descriptions=tuple(t.postcondition_descriptions),
                postconditions=tuple(
                    PostconditionRef(
                        name=pc.name,
                        args=tuple(sorted(pc.args.items())),
                    )
                    for pc in t.postconditions
                ),
                output_path=t.output_path,
                stages=tuple(
                    _validate_and_build_stage(s, t.id)
                    for s in t.stages
                ),
            )
        )

    return Flow(
        name=schema.name,
        description=schema.description,
        brief=schema.brief,
        api_spec=schema.api_spec,
        agents=tuple(agents_list),
        task_graph=tuple(included_tasks + own_tasks),
    )


def _validate_and_build_stage(
    s: _StageSchema, task_id: str
) -> StageTemplate:
    """Build a StageTemplate from its schema row, enforcing kind-specific rules."""
    kind = s.kind or "llm"
    _FRAMEWORK_KINDS = {
        "framework_finalize_plan",
        "plan_reset_tasks",
        "plan_reset_agents",
        "plan_snapshot_draft",
        "plan_validate_agent",
        "plan_validate_roster",
        "plan_validate_agents_vs_tasks",
        "plan_link_tasks_to_agents",
        "plan_scaffold_tests",
        "plan_run_pytest",
        # v2.6 — per-deliverable LLM call that derives test intent from the
        # brief. Splits "read brief + infer intent + write code" into narrow
        # cognitive slices that 7B can handle.
        "plan_derive_test_intent",
    }
    _ALLOWED_KINDS = {"llm", "decision"} | _FRAMEWORK_KINDS
    if kind not in _ALLOWED_KINDS:
        raise AgoraError(
            f"task {task_id} stage {s.name!r}: unknown kind {kind!r} "
            f"(expected one of {sorted(_ALLOWED_KINDS)})"
        )
    if kind == "decision":
        if not s.decision_id:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: decision stages require decision_id"
            )
        if not s.question:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: decision stages require question"
            )
        if len(s.options) < 2:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: decision stages require ≥ 2 options"
            )
        if not s.output_path:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: decision stages require output_path"
            )
        if s.instruction:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: decision stages cannot declare "
                f"an instruction (that would mix LLM and decision modes)"
            )
    elif kind == "framework_finalize_plan":
        if not s.output_path:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: framework_finalize_plan stages "
                f"require output_path (where the emitted plan YAML is written)"
            )
        if s.instruction:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: framework_finalize_plan stages "
                f"cannot declare an instruction (no LLM is invoked)"
            )
    elif kind in {"plan_reset_tasks", "plan_reset_agents", "plan_validate_roster",
                  "plan_validate_agents_vs_tasks", "plan_link_tasks_to_agents"}:
        # Idempotent framework stages with no required args. Instruction
        # must be empty (no LLM is invoked).
        if s.instruction:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: {kind} stages "
                f"cannot declare an instruction (no LLM is invoked)"
            )
    elif kind == "plan_snapshot_draft":
        if not s.output_path:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: plan_snapshot_draft stages "
                f"require output_path (where the draft markdown is written)"
            )
        if s.instruction:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: plan_snapshot_draft stages "
                f"cannot declare an instruction (no LLM is invoked)"
            )
    elif kind == "plan_scaffold_tests":
        if not s.output_path:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: plan_scaffold_tests stages "
                f"require output_path (where the test file skeleton is written)"
            )
        if s.instruction:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: plan_scaffold_tests stages "
                f"cannot declare an instruction (no LLM is invoked)"
            )
    elif kind == "plan_run_pytest":
        # output_path is optional — defaults to plan/kb/pytest_output.md at
        # runtime. Instruction must be empty since no LLM is invoked.
        if s.instruction:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: plan_run_pytest stages "
                f"cannot declare an instruction (no LLM is invoked)"
            )
    elif kind == "plan_validate_agent":
        if s.instruction:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: plan_validate_agent stages "
                f"cannot declare an instruction (no LLM is invoked)"
            )
        if "expected_name" not in s.validation_args:
            raise AgoraError(
                f"task {task_id} stage {s.name!r}: plan_validate_agent stages "
                f"require validation_args.expected_name (the agent's name)"
            )
    return StageTemplate(
        name=s.name,
        instruction=s.instruction,
        context_files=tuple(s.context_files),
        max_iterations=s.max_iterations,
        kind=kind,
        decision_id=s.decision_id,
        question=s.question,
        options=tuple(s.options),
        output_path=s.output_path,
        validation_args=tuple(sorted(s.validation_args.items())),
        hide_tools=tuple(s.hide_tools),
    )


def _dedupe_agents(agents: list[AgentConfig]) -> list[AgentConfig]:
    """Preserve order, keep the last definition for a given name."""
    seen: dict[str, AgentConfig] = {}
    order: list[str] = []
    for a in agents:
        if a.name not in seen:
            order.append(a.name)
        seen[a.name] = a
    return [seen[n] for n in order]


def save_flow(flow: Flow, path: str | Path) -> None:
    """Serialize a flow to YAML.

    Uses version ``2.0`` iff any task carries v2.0-only fields (typed
    postconditions, output_path, or stages); otherwise emits v1.0 for
    backward compatibility with existing fixtures.
    """
    needs_v2 = any(
        t.postconditions or t.output_path or t.stages for t in flow.task_graph
    )
    version = "2.0" if needs_v2 else "1.0"

    task_rows: list[dict[str, Any]] = []
    for t in flow.task_graph:
        row: dict[str, Any] = {
            "id": t.id,
            "assigned_to": t.assigned_to,
            "description": t.description,
            "depends_on": list(t.depends_on),
            "precondition_descriptions": list(t.precondition_descriptions),
            "postcondition_descriptions": list(t.postcondition_descriptions),
        }
        if t.postconditions:
            row["postconditions"] = [
                {"name": pc.name, "args": pc.args_dict()} for pc in t.postconditions
            ]
        if t.output_path:
            row["output_path"] = t.output_path
        if t.stages:
            stage_rows: list[dict[str, Any]] = []
            for s in t.stages:
                sr: dict[str, Any] = {"name": s.name}
                if s.kind and s.kind != "llm":
                    sr["kind"] = s.kind
                if s.instruction:
                    sr["instruction"] = s.instruction
                if s.context_files:
                    sr["context_files"] = list(s.context_files)
                sr["max_iterations"] = s.max_iterations
                if s.decision_id:
                    sr["decision_id"] = s.decision_id
                if s.question:
                    sr["question"] = s.question
                if s.options:
                    sr["options"] = list(s.options)
                if s.output_path:
                    sr["output_path"] = s.output_path
                if s.validation_args:
                    sr["validation_args"] = dict(s.validation_args)
                if s.hide_tools:
                    sr["hide_tools"] = list(s.hide_tools)
                stage_rows.append(sr)
            row["stages"] = stage_rows
        task_rows.append(row)

    data = {
        "version": version,
        "name": flow.name,
        "description": flow.description,
        **({"brief": flow.brief} if flow.brief else {}),
        **({"api_spec": flow.api_spec} if flow.api_spec else {}),
        "agents": [
            {
                "name": a.name,
                "role": a.role.value,
                "model": a.model,
                "instructions": a.instructions,
                "knowledge_files": list(a.knowledge_files),
            }
            for a in flow.agents
        ],
        "task_graph": task_rows,
    }
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def instantiate_flow(
    flow: Flow,
    project_name: str,
    variables: dict[str, str] | None = None,
    *,
    id_strategy: str = "uuid",
) -> tuple[list[AgentConfig], list[Task]]:
    """Concrete AgentConfigs + Tasks from a flow template, with ``${var}`` substitution.

    Always-available variables: ``project_name``. Callers may pass more via ``variables``
    (e.g. ``{"repo_path": "./workspace/repo"}``). Any ``${unknown}`` token in a string
    field raises :class:`AgoraError`.

    ``id_strategy``:
        - ``"uuid"`` (default, preserves v1.0 behavior): task ids become fresh UUIDs;
          ``depends_on`` is remapped.
        - ``"preserve"`` (used by :mod:`agora.plan.loader`): task ids are kept
          as the template's string ids so a staged-task dict keyed by those ids
          still matches.

    For tasks with typed ``postconditions`` (v2.0 plans), each reference is resolved
    via :mod:`agora.plan.predicate_registry`. Tasks with only
    ``postcondition_descriptions`` fall back to the ``_always_true`` stub.
    """
    from string import Template

    if id_strategy not in {"uuid", "preserve"}:
        raise AgoraError(
            f"unknown id_strategy {id_strategy!r}; expected 'uuid' or 'preserve'"
        )

    env: dict[str, str] = {"project_name": project_name}
    if variables:
        env.update(variables)

    def _sub(value: str) -> str:
        try:
            return Template(value).substitute(env)
        except KeyError as exc:
            raise AgoraError(
                f"flow '{flow.name}' references unknown variable ${{{exc.args[0]}}}"
            ) from exc
        except ValueError as exc:
            raise AgoraError(f"flow '{flow.name}' has malformed template: {exc}") from exc

    # Substitute strings on the agents first. ``model`` is substituted too so
    # plan YAML can use ``model: ${model}`` and have the runner inject the
    # concrete model name at load time (e.g. ``ollama/qwen2.5:7b-instruct``).
    agents = [
        AgentConfig(
            name=a.name,
            role=a.role,
            model=_sub(a.model),
            instructions=_sub(a.instructions),
            knowledge_files=tuple(_sub(f) for f in a.knowledge_files),
        )
        for a in flow.agents
    ]

    if id_strategy == "uuid":
        id_map: dict[str, str] = {t.id: str(uuid.uuid4()) for t in flow.task_graph}
    else:
        id_map = {t.id: t.id for t in flow.task_graph}

    now = datetime.now(UTC).isoformat()

    tasks: list[Task] = []
    for t in flow.task_graph:
        desc = _sub(t.description)
        postconditions = _build_postconditions(t, flow.name)
        spec = Specification(
            preconditions=tuple(
                make_predicate(f"pre_{i}", _sub(d), _always_true)
                for i, d in enumerate(t.precondition_descriptions)
            ),
            postconditions=postconditions,
            description=f"[{project_name}] {desc}",
        )
        tasks.append(
            Task(
                id=id_map[t.id],
                spec=spec,
                description=desc,
                agent_id=t.assigned_to,
                depends_on=tuple(id_map[d] for d in t.depends_on),
                status=TaskStatus.PENDING,
                created_at=now,
                updated_at=now,
                output_path=t.output_path,
            )
        )
    return agents, tasks


def _build_postconditions(
    template: TaskTemplate, flow_name: str
) -> tuple[Any, ...]:
    """Prefer typed postconditions (v2.0) via the registry; fall back to description stubs."""
    if template.postconditions:
        from agora.plan.predicate_registry import build_predicate

        try:
            return tuple(
                build_predicate(ref.name, ref.args_dict())
                for ref in template.postconditions
            )
        except AgoraError as exc:
            raise AgoraError(
                f"flow '{flow_name}' task {template.id!r}: {exc}"
            ) from exc
    return tuple(
        make_predicate(f"post_{i}", d, _always_true)
        for i, d in enumerate(template.postcondition_descriptions)
    )


def _always_true(_ctx: dict[str, Any]) -> tuple[bool, str]:
    return True, ""
