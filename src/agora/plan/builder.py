"""Mutable in-memory plan state for tool-driven plan authoring.

An :class:`LLM`-facing plan-authoring flow mutates a :class:`PlanDraft` via
typed inner tools (``plan_add_task``, ``plan_attach_postcondition`` etc).
The framework owns YAML serialization — the model never authors YAML. At
``plan_finalize`` the draft converts to a frozen :class:`~agora.core.flow.Flow`
which is saved via :func:`agora.core.flow.save_flow` and round-tripped
through :func:`agora.plan.loader.load_plan` + ``instantiate_plan`` to prove
the emitted YAML is executable.

Design notes
------------

- Task insertion order is preserved via an ``OrderedDict`` so the emitted
  YAML is deterministic and diff-friendly.
- Validation happens at the tool boundary. Each ``add_*`` / ``attach_*``
  method raises :class:`~agora.core.errors.AgoraError` with a human-readable
  message the tool executor surfaces back to the LLM. The LLM can then
  correct on its next turn — standard feedback loop.
- Stages are stored as plain dicts (shape mirrors ``StageTemplate``). They
  convert to real ``StageTemplate`` objects only at ``to_flow()`` time.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from agora.core.agent import AgentConfig
from agora.core.errors import AgoraError
from agora.core.flow import (
    Flow,
    PostconditionRef,
    StageTemplate,
    TaskTemplate,
)
from agora.core.task import build_dag
from agora.core.types import AgentRole

_ALLOWED_STAGE_KINDS = frozenset({"llm", "decision"})


@dataclass
class PlanDraft:
    """Mutable plan being authored by an LLM agent via typed tool calls."""

    name: str = ""
    description: str = ""
    agents: list[dict[str, Any]] = field(default_factory=list)
    # Ordered by insertion so the emitted YAML task_graph stays deterministic.
    tasks: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    # v2.6 — rich brief text (markdown). Populated at finalize time by reading
    # the plan-builder's ``plan/brief.md`` into memory so it travels with the
    # emitted plan.yaml and reaches the executor workspace's scaffolder.
    brief: str = ""
    # v2.7 — shared API spec. Populated at finalize time from the plan-builder's
    # ``plan/api_spec.md``. Defines module surface (class names, function
    # signatures) as the single source of truth for tester + implementer.
    api_spec: str = ""

    # --------------------------------------------------------------- metadata

    def set_metadata(self, name: str, description: str = "") -> None:
        if not name:
            raise AgoraError("plan name must be non-empty")
        self.name = name
        self.description = description or ""

    # ----------------------------------------------------------------- agents

    def set_agents(self, agents: list[dict[str, Any]]) -> None:
        """Replace the agent roster in one shot. ``agents`` is a list of
        ``{name, role, instructions, model?}`` dicts. Validates roles and
        deduplicates by name.
        """
        if not isinstance(agents, list) or not agents:
            raise AgoraError("plan_set_agents requires a non-empty list")
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for i, entry in enumerate(agents):
            if not isinstance(entry, dict):
                raise AgoraError(f"agent[{i}] must be a dict, got {type(entry).__name__}")
            name = str(entry.get("name", "")).strip()
            role_raw = str(entry.get("role", "")).strip()
            if not name:
                raise AgoraError(f"agent[{i}] missing 'name'")
            if name in seen:
                raise AgoraError(f"duplicate agent name {name!r}")
            seen.add(name)
            try:
                role = AgentRole(role_raw)
            except ValueError as exc:
                valid = sorted(r.value for r in AgentRole)
                raise AgoraError(
                    f"agent[{i}]={name!r}: unknown role {role_raw!r} — expected one of {valid}"
                ) from exc
            normalized.append(
                {
                    "name": name,
                    "role": role.value,
                    "instructions": str(entry.get("instructions", "")),
                    "model": str(entry.get("model", "") or ""),
                }
            )
        self.agents = normalized

    def _agent_names(self) -> set[str]:
        return {a["name"] for a in self.agents}

    def upsert_agent(
        self,
        name: str,
        role: str,
        instructions: str,
        model: str = "",
    ) -> bool:
        """Add OR replace one agent by name. Idempotent on the name key.

        Returns True if a new agent was appended, False if an existing
        entry was updated. This is the surface the per-agent author
        stages call — each stage's narrow scope is "define one agent",
        and upsert lets a stage retry correct a prior mistake without
        clobbering the rest of the roster.
        """
        clean_name = str(name or "").strip()
        if not clean_name:
            raise AgoraError("agent name must be non-empty")
        role_raw = str(role or "").strip()
        try:
            role_enum = AgentRole(role_raw)
        except ValueError as exc:
            valid = sorted(r.value for r in AgentRole)
            raise AgoraError(
                f"agent {clean_name!r}: unknown role {role_raw!r} — expected one of {valid}"
            ) from exc
        entry = {
            "name": clean_name,
            "role": role_enum.value,
            "instructions": str(instructions or ""),
            "model": str(model or ""),
        }
        for i, existing in enumerate(self.agents):
            if existing["name"] == clean_name:
                self.agents[i] = entry
                return False
        self.agents.append(entry)
        return True

    def reset_agents(self) -> int:
        """Clear the agents roster. Returns the prior count so callers can
        log it. Mirrors :meth:`_autofill_missing_postconditions` / the
        ``plan_reset_tasks`` pattern — framework-only, no validation."""
        prior = len(self.agents)
        self.agents.clear()
        return prior

    # ----------------------------------------------------- per-agent validation

    #: Per-agent compile-check: instructions must be this many chars at minimum
    #: to count as "authored". Below that, we assume the model produced a
    #: placeholder / template fragment. 20 chars ≈ one short sentence.
    _MIN_AGENT_INSTRUCTIONS_CHARS = 20

    def validate_agent(
        self,
        name: str,
        *,
        expected_role: str = "",
    ) -> list[str]:
        """Per-agent compile check. Returns the list of problems (empty =
        valid). Used both by the ``plan_validate_agent`` framework stage
        and by the ``plan_draft_all_agents_valid`` postcondition.

        Checks:
          - an agent with ``name`` exists in the draft
          - role is a known :class:`AgentRole` value
          - if ``expected_role`` is given, actual role matches
          - ``instructions`` is at least :attr:`_MIN_AGENT_INSTRUCTIONS_CHARS`
            chars long (catches "" / template defaults)
        """
        problems: list[str] = []
        match = next((a for a in self.agents if a["name"] == name), None)
        if match is None:
            problems.append(f"agent {name!r} is not in the draft roster")
            return problems
        role = match.get("role", "")
        try:
            AgentRole(role)
        except ValueError:
            problems.append(f"agent {name!r}: invalid role {role!r}")
        if expected_role and role != expected_role:
            problems.append(
                f"agent {name!r}: expected role {expected_role!r}, got {role!r}"
            )
        instructions = str(match.get("instructions", ""))
        if len(instructions.strip()) < self._MIN_AGENT_INSTRUCTIONS_CHARS:
            problems.append(
                f"agent {name!r}: instructions too short "
                f"({len(instructions.strip())} < {self._MIN_AGENT_INSTRUCTIONS_CHARS} chars) — "
                f"write 2-4 sentences describing responsibility"
            )
        return problems

    _BUILDER_ROLES = frozenset({"architect", "implementer"})

    def validate_roster(self, *, min_agents: int = 2) -> list[str]:
        """Roster-level checks. Returns a list of problems.

        - minimum count
        - at least one agent with a "builder" role (architect or implementer)
          so the plan actually has someone to produce deliverables
        - no duplicate ``(name, role)`` pairs (name uniqueness is enforced on
          upsert, but a rogue direct-mutation could still produce dupes)
        """
        problems: list[str] = []
        if len(self.agents) < min_agents:
            problems.append(
                f"roster has {len(self.agents)} agent(s), need ≥ {min_agents}"
            )
        roles = {a.get("role", "") for a in self.agents}
        if not (roles & self._BUILDER_ROLES):
            problems.append(
                "roster has no builder role (architect or implementer) — "
                "the plan needs at least one agent that can produce artifacts"
            )
        seen: set[tuple[str, str]] = set()
        for a in self.agents:
            key = (a.get("name", ""), a.get("role", ""))
            if key in seen:
                problems.append(f"duplicate (name, role) pair: {key}")
            seen.add(key)
        return problems

    #: Heuristic signals in task id/description/output_path that suggest a
    #: specific role owner. Matched case-insensitively via substring. Order
    #: matters: earlier entries win for tasks that hit multiple signals.
    _ROLE_HEURISTICS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("tester", ("test_", "tests/", "/tests/", "pytest", "unittest", "_test")),
        ("architect", (
            "setup", "scaffold", "skeleton", "requirements.txt", "pyproject",
            "__init__.py", "config", "manifest", "architecture", "outline",
        )),
        # implementer is the default fallback when nothing else matches.
    )

    def _suggest_role(self, task_row: dict[str, Any]) -> str:
        """Return the role-hint for a task based on its id/output_path/description.

        Pure heuristic — safe for the linker to use as a fallback when the
        model didn't explicitly express agent intent. Returns ``"implementer"``
        when nothing matches (the default builder).
        """
        haystack = " ".join(
            str(task_row.get(k, "")) for k in ("description", "output_path")
        ).lower()
        # Include task_id when it's stored at a higher level; the linker
        # passes it through task_row["_id"] for this reason.
        haystack += " " + str(task_row.get("_id", "")).lower()
        for role, needles in self._ROLE_HEURISTICS:
            for needle in needles:
                if needle in haystack:
                    return role
        return "implementer"

    def link_tasks_to_agents(self) -> dict[str, list[str]]:
        """Re-balance task assignments so every agent has ≥1 task when
        possible. Returns a dict of ``{action: [task_ids]}`` for logging.

        Phase 1 — *Fill gaps*: any task with empty ``assigned_to`` gets a
        role-based suggestion matched against the roster. If no agent with
        that role exists, the first builder-role agent is used.

        Phase 2 — *Rebalance orphans*: for each agent that still has no
        task assigned, steal the first task from an agent who has ≥2 tasks
        AND whose role matches the orphan's role (or, failing that, any
        multi-task agent).

        Never overwrites an explicit ``assigned_to`` when the named agent
        exists in the roster.
        """
        actions: dict[str, list[str]] = {"filled": [], "rebalanced": []}
        names = self._agent_names()
        if not names or not self.tasks:
            return actions
        by_role: dict[str, list[str]] = {}
        for a in self.agents:
            by_role.setdefault(a["role"], []).append(a["name"])
        first_builder = next(
            (a["name"] for a in self.agents if a["role"] in self._BUILDER_ROLES),
            next((a["name"] for a in self.agents), ""),
        )

        # Phase 1 — fill gaps.
        for tid, row in self.tasks.items():
            if str(row.get("assigned_to", "")) in names:
                continue
            row["_id"] = tid  # transient hint for _suggest_role
            role = self._suggest_role(row)
            row.pop("_id", None)
            candidates = by_role.get(role, [])
            row["assigned_to"] = candidates[0] if candidates else first_builder
            actions["filled"].append(tid)

        # Phase 2 — rebalance orphan agents.
        assigned_counts: dict[str, int] = {n: 0 for n in names}
        for row in self.tasks.values():
            owner = row.get("assigned_to", "")
            if owner in assigned_counts:
                assigned_counts[owner] += 1
        orphans = [n for n, c in assigned_counts.items() if c == 0]
        if not orphans:
            return actions
        agent_role: dict[str, str] = {a["name"]: a["role"] for a in self.agents}
        for orphan in orphans:
            target_role = agent_role.get(orphan, "")
            # Prefer stealing a task whose role matches the orphan's role,
            # from an owner with >1 tasks.
            best_tid: str | None = None
            for tid, row in self.tasks.items():
                owner = row.get("assigned_to", "")
                if owner == orphan or assigned_counts.get(owner, 0) <= 1:
                    continue
                row["_id"] = tid
                if self._suggest_role(row) == target_role:
                    best_tid = tid
                    row.pop("_id", None)
                    break
                row.pop("_id", None)
            # Fallback: any task from an over-loaded owner.
            if best_tid is None:
                for tid, row in self.tasks.items():
                    owner = row.get("assigned_to", "")
                    if owner != orphan and assigned_counts.get(owner, 0) > 1:
                        best_tid = tid
                        break
            if best_tid is None:
                # No rebalance possible (every other agent has ≤1 task).
                continue
            prior_owner = self.tasks[best_tid]["assigned_to"]
            self.tasks[best_tid]["assigned_to"] = orphan
            assigned_counts[prior_owner] -= 1
            assigned_counts[orphan] += 1
            actions["rebalanced"].append(best_tid)
        return actions

    def validate_agents_vs_tasks(self) -> list[str]:
        """Coverage cross-check between agents and tasks.

        - every agent has ≥ 1 task assigned (else why declare?)
        - every task's ``assigned_to`` is a known agent
        - every task has a non-empty ``assigned_to``

        Returns the problems list; empty → OK. Called by the
        ``plan_validate_agents_vs_tasks`` framework stage that runs after
        ``author_tasks`` but before ``finalize_plan``.
        """
        problems: list[str] = []
        agent_names = self._agent_names()
        task_assignees: set[str] = set()
        for tid, t in self.tasks.items():
            assigned = str(t.get("assigned_to", ""))
            if not assigned:
                problems.append(f"task {tid!r} has no assigned_to")
                continue
            if assigned not in agent_names:
                problems.append(
                    f"task {tid!r} assigned_to {assigned!r} is not in the "
                    f"agent roster (have: {sorted(agent_names)})"
                )
                continue
            task_assignees.add(assigned)
        orphans = sorted(agent_names - task_assignees)
        if orphans:
            problems.append(
                f"agents with no tasks assigned: {orphans} — "
                f"either assign them work or remove them from the roster"
            )
        return problems

    def snapshot_markdown(self) -> str:
        """Human-readable snapshot of the current draft for cross-stage
        context injection. Each author stage's next-stage reads this via
        ``context_files`` so the LLM can see what task_ids exist when
        choosing ``depends_on`` values, etc.
        """
        lines: list[str] = ["# Plan Draft State", ""]
        lines.append(f"## Agents ({len(self.agents)})")
        if not self.agents:
            lines.append("_(none yet)_")
        else:
            for a in self.agents:
                instr = str(a.get("instructions", "")).strip().splitlines()
                first = instr[0] if instr else ""
                if len(first) > 120:
                    first = first[:117] + "…"
                lines.append(
                    f"- **{a['name']}** (role={a['role']}) — {first or '(no instructions)'}"
                )
        lines.append("")
        lines.append(f"## Tasks ({len(self.tasks)})")
        if not self.tasks:
            lines.append("_(none yet)_")
        else:
            for tid, t in self.tasks.items():
                deps = t.get("depends_on") or []
                dep_str = ", ".join(deps) if deps else "none"
                pc_names = [pc["name"] for pc in t.get("postconditions", [])]
                pc_str = ", ".join(pc_names) if pc_names else "(none)"
                desc = str(t.get("description", "")).strip().splitlines()
                first = desc[0] if desc else ""
                if len(first) > 120:
                    first = first[:117] + "…"
                lines.append(
                    f"- **{tid}** → {t.get('assigned_to', '?')}; "
                    f"depends_on=[{dep_str}]; postconditions=[{pc_str}]"
                )
                if first:
                    lines.append(f"  _{first}_")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ tasks

    def add_task(
        self,
        task_id: str,
        description: str,
        assigned_to: str,
        depends_on: list[str] | None = None,
        output_path: str = "",
    ) -> None:
        if not task_id or not isinstance(task_id, str):
            raise AgoraError("task_id must be a non-empty string")
        if task_id in self.tasks:
            raise AgoraError(f"task {task_id!r} already exists")
        if not self.agents:
            raise AgoraError(
                "plan_set_agents must be called before plan_add_task"
            )
        if assigned_to not in self._agent_names():
            valid = sorted(self._agent_names())
            raise AgoraError(
                f"task {task_id!r} assigned_to {assigned_to!r} is not a registered agent "
                f"(valid: {valid})"
            )
        deps = list(depends_on or [])
        for d in deps:
            if d not in self.tasks:
                raise AgoraError(
                    f"task {task_id!r} depends on {d!r} which does not exist yet "
                    f"(add dependencies before their dependents)"
                )
        self.tasks[task_id] = {
            "description": str(description),
            "assigned_to": assigned_to,
            "depends_on": deps,
            "output_path": str(output_path or ""),
            "postconditions": [],
            "stages": [],
        }

    def add_task_spec(
        self,
        task_id: str,
        description: str,
        assigned_to: str,
        depends_on: list[str] | None = None,
        output_path: str = "",
        postconditions: list[dict[str, Any]] | None = None,
        api_spec_modules: set[str] | None = None,
    ) -> int:
        """Atomic: add a task AND attach its postconditions in one call.

        The compound operation is the tool surface that weak models can
        actually use reliably — per-task stages invoke this once instead of
        looping ``plan_add_task`` then ``plan_attach_postcondition`` N times.
        If any postcondition is malformed, the whole task is rolled back so
        the draft never ends up in a half-authored state.

        v2.8 (Approach C — C4a): if ``api_spec_modules`` is non-empty, any
        ``src/*.py`` path referenced by ``output_path`` or by a postcondition
        ``rel`` must appear in the set. Surfaces the architect's "I'm
        targeting a module that was never declared" mistake at authoring
        time rather than silently at finalize-time remapping/dropping.
        Non-src paths (tests/*, requirements.txt, etc.) are never gated.

        Also auto-fills the ``rel`` arg for any postcondition whose factory
        requires it (``file_exists``, ``py_compiles``, ``file_contains``,
        ``no_code_after_main_block``, ``max_line_length``, ``python_imports``)
        when the caller didn't pass one and the task has an ``output_path``.
        Lets weak models pass a minimal ``[{"name": "file_exists"}]`` list
        instead of ``[{"name": "file_exists", "args": {"rel": <path>}}]``.

        Returns the number of postconditions attached.
        """
        pcs = list(postconditions or [])
        # v2.8(C4a): spec-membership pre-check BEFORE mutating state. If the
        # task's output_path targets an unknown src/* module, reject with a
        # structured message naming the known modules. Non-src paths pass
        # through unchecked (they're not scaffolded from api_spec).
        _validate_src_path_in_api_spec(
            output_path, pcs, api_spec_modules, task_id=task_id
        )
        self.add_task(task_id, description, assigned_to, depends_on, output_path)
        attached = 0
        try:
            for i, pc in enumerate(pcs):
                if not isinstance(pc, dict):
                    raise AgoraError(
                        f"postconditions[{i}] must be a dict, "
                        f"got {type(pc).__name__}"
                    )
                name = str(pc.get("name", "")).strip()
                args = pc.get("args") or {}
                if not isinstance(args, dict):
                    raise AgoraError(f"postconditions[{i}].args must be an object")
                args = _autofill_postcondition_args(name, dict(args), output_path)
                self.attach_postcondition(task_id, name, args)
                attached += 1
        except AgoraError:
            # Roll the task back so the draft is unchanged on failure.
            self.tasks.pop(task_id, None)
            raise
        return attached

    # ---------------------------------------------------------- postconditions

    def attach_postcondition(
        self,
        task_id: str,
        name: str,
        args: dict[str, Any] | None = None,
    ) -> None:
        if task_id not in self.tasks:
            raise AgoraError(f"unknown task_id {task_id!r}")
        if not name:
            raise AgoraError("postcondition name must be non-empty")
        # Validate by construction — raises if the registry doesn't know the name
        # or the args don't match the factory's signature.
        from agora.plan.predicate_registry import build_predicate

        args = dict(args or {})
        try:
            build_predicate(name, args)
        except TypeError as exc:
            # Factory kwargs mismatch.
            raise AgoraError(
                f"postcondition {name!r} args invalid: {exc}"
            ) from exc
        self.tasks[task_id]["postconditions"].append({"name": name, "args": args})

    # ----------------------------------------------------------------- stages

    def _stage_names(self, task_id: str) -> set[str]:
        return {s.get("name", "") for s in self.tasks[task_id]["stages"]}

    def add_llm_stage(
        self,
        task_id: str,
        name: str,
        instruction: str,
        context_files: list[str] | None = None,
        max_iterations: int = 5,
    ) -> None:
        if task_id not in self.tasks:
            raise AgoraError(f"unknown task_id {task_id!r}")
        if not name:
            raise AgoraError("stage name must be non-empty")
        if name in self._stage_names(task_id):
            raise AgoraError(
                f"task {task_id!r} already has a stage named {name!r}"
            )
        if not instruction:
            raise AgoraError("llm stage requires non-empty instruction")
        if max_iterations < 1:
            raise AgoraError("max_iterations must be ≥ 1")
        self.tasks[task_id]["stages"].append(
            {
                "name": name,
                "kind": "llm",
                "instruction": str(instruction),
                "context_files": list(context_files or []),
                "max_iterations": int(max_iterations),
            }
        )

    def add_decision_stage(
        self,
        task_id: str,
        name: str,
        decision_id: str,
        question: str,
        options: list[str],
        output_path: str,
    ) -> None:
        if task_id not in self.tasks:
            raise AgoraError(f"unknown task_id {task_id!r}")
        if not name:
            raise AgoraError("stage name must be non-empty")
        if name in self._stage_names(task_id):
            raise AgoraError(
                f"task {task_id!r} already has a stage named {name!r}"
            )
        if not decision_id:
            raise AgoraError("decision stage requires decision_id")
        # Enforce global uniqueness of decision_ids across the plan.
        for existing_tid, t in self.tasks.items():
            for s in t["stages"]:
                if s.get("kind") == "decision" and s.get("decision_id") == decision_id:
                    raise AgoraError(
                        f"decision_id {decision_id!r} already used in task "
                        f"{existing_tid!r} stage {s.get('name')!r}"
                    )
        if not question:
            raise AgoraError("decision stage requires question")
        opts = list(options or [])
        if len(opts) < 2:
            raise AgoraError("decision stage requires ≥ 2 options")
        if not output_path:
            raise AgoraError("decision stage requires output_path")
        self.tasks[task_id]["stages"].append(
            {
                "name": name,
                "kind": "decision",
                "decision_id": str(decision_id),
                "question": str(question),
                "options": [str(o) for o in opts],
                "output_path": str(output_path),
            }
        )

    # -------------------------------------------------------------- readiness

    def validate_ready(self) -> list[str]:
        """Return a list of structural problems that would prevent finalize.

        Empty list → ready. Non-empty → ``plan_finalize`` should surface the
        issues as an error string and let the LLM retry.

        Checks: ≥ 1 agent, ≥ 1 task, every task has ≥ 1 postcondition, DAG is
        acyclic. The compound ``plan_add_task_spec`` tool makes it easy for
        weak models to satisfy the postcondition rule — each per-task stage
        authors one complete task (spec + postconditions) via a single call.
        """
        problems: list[str] = []
        if not self.agents:
            problems.append("no agents set (call plan_set_agents first)")
        if not self.tasks:
            problems.append("no tasks added (call plan_add_task)")
        missing_pc = [tid for tid, t in self.tasks.items() if not t["postconditions"]]
        if missing_pc:
            problems.append(
                f"tasks without postconditions: {missing_pc[:5]}"
                + (" …" if len(missing_pc) > 5 else "")
            )
        try:
            stub_tasks = self._stub_tasks_for_dag_check()
            build_dag(stub_tasks)
        except AgoraError as exc:
            problems.append(f"invalid DAG: {exc}")
        return problems

    def _stub_tasks_for_dag_check(self):
        """Build minimal ``Task`` objects just for ``build_dag`` to traverse."""
        from agora.core.contract import Specification
        from agora.core.task import Task
        from agora.core.types import TaskStatus

        stubs = []
        for tid, row in self.tasks.items():
            stubs.append(
                Task(
                    id=tid,
                    spec=Specification(),
                    description=row["description"],
                    agent_id=row["assigned_to"],
                    depends_on=tuple(row["depends_on"]),
                    status=TaskStatus.PENDING,
                )
            )
        return stubs

    # ----------------------------------------------------------- serialization

    def to_flow(self) -> Flow:
        """Convert the draft to a frozen :class:`Flow` ready for ``save_flow``.

        Does NOT re-validate readiness — callers should call
        :meth:`validate_ready` first and handle any problems.
        """
        agent_configs = tuple(
            AgentConfig(
                name=a["name"],
                role=AgentRole(a["role"]),
                model=a["model"] or "",
                instructions=a["instructions"],
            )
            for a in self.agents
        )
        task_templates = tuple(
            TaskTemplate(
                id=tid,
                assigned_to=row["assigned_to"],
                description=row["description"],
                depends_on=tuple(row["depends_on"]),
                postconditions=tuple(
                    PostconditionRef(
                        name=pc["name"],
                        args=tuple(sorted(pc["args"].items())),
                    )
                    for pc in row["postconditions"]
                ),
                output_path=row["output_path"],
                stages=tuple(self._stage_to_template(s) for s in row["stages"]),
            )
            for tid, row in self.tasks.items()
        )
        return Flow(
            name=self.name or "planned",
            description=self.description,
            brief=self.brief,
            api_spec=self.api_spec,
            agents=agent_configs,
            task_graph=task_templates,
        )

    @staticmethod
    def _stage_to_template(s: dict[str, Any]) -> StageTemplate:
        kind = s.get("kind", "llm")
        if kind not in _ALLOWED_STAGE_KINDS:
            raise AgoraError(f"unknown stage kind {kind!r}")
        return StageTemplate(
            name=s["name"],
            instruction=s.get("instruction", ""),
            context_files=tuple(s.get("context_files", ())),
            max_iterations=int(s.get("max_iterations", 5)),
            kind=kind,
            decision_id=s.get("decision_id", ""),
            question=s.get("question", ""),
            options=tuple(s.get("options", ())),
            output_path=s.get("output_path", ""),
        )

    # --------------------------------------------------------------- summary

    def summary(self) -> str:
        """Short human-readable summary for tool-result strings."""
        pc_total = sum(len(t["postconditions"]) for t in self.tasks.values())
        st_total = sum(len(t["stages"]) for t in self.tasks.values())
        return (
            f"plan_draft: name={self.name!r} agents={len(self.agents)} "
            f"tasks={len(self.tasks)} postconditions={pc_total} stages={st_total}"
        )


def _collect_src_paths(
    output_path: str, postconditions: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    """Return ``(source, path)`` tuples for every production-Python path
    referenced by the task-spec-in-progress. ``source`` is either
    ``"output_path"`` or ``"postcondition:<name>"`` for diagnostic messages.

    A path counts as a production-Python reference when it ends with
    ``.py`` AND is not a test path (``tests/...`` or ``src/tests/...``).
    v2.9(C4a): the scope was broadened from src-only to any non-test
    Python path. Observed bug 2026-04-22 PM: the architect emitted an
    impl task with ``output_path: plan/core_domain_module.py``, which
    the src-only filter let through — the executor then wrote code to
    the planner's own workspace and contract tests ``from src.url_shortener``
    failed. Requiring EVERY production Python path be in api_spec_modules
    forces the architect to either (a) add the module to api_spec, or
    (b) retarget the task to an existing module.

    Non-Python files (``requirements.txt``, ``README.md``) are ignored —
    they're not api_spec concerns.
    """

    def _is_production_py(p: str) -> bool:
        norm = p.replace("\\", "/")
        if not norm.endswith(".py"):
            return False
        if norm.startswith("tests/") or norm.startswith("src/tests/"):
            return False
        return True

    out: list[tuple[str, str]] = []
    if _is_production_py(output_path):
        out.append(("output_path", output_path.replace("\\", "/")))
    for i, pc in enumerate(postconditions or ()):
        if not isinstance(pc, dict):
            continue
        args = pc.get("args") or {}
        if not isinstance(args, dict):
            continue
        rel = str(args.get("rel", "") or "")
        if _is_production_py(rel):
            out.append((f"postconditions[{i}].args.rel", rel.replace("\\", "/")))
    return out


def _validate_src_path_in_api_spec(
    output_path: str,
    postconditions: list[dict[str, Any]],
    api_spec_modules: set[str] | None,
    *,
    task_id: str,
) -> None:
    """Raise :class:`AgoraError` if any src/*.py path on the task-in-progress
    is not in ``api_spec_modules``. No-op when ``api_spec_modules`` is None
    (caller opted out, e.g. no api_spec authored yet) or empty (no modules
    declared yet — other postconditions surface that).
    """
    if not api_spec_modules:
        return
    refs = _collect_src_paths(output_path, postconditions or [])
    if not refs:
        return
    known = {m.replace("\\", "/") for m in api_spec_modules}
    bad: list[tuple[str, str]] = [(src, p) for src, p in refs if p not in known]
    if not bad:
        return
    # One message per offending ref — give the model full visibility.
    known_list = sorted(known)
    lines = [
        f"task {task_id!r} references Python module path(s) not declared "
        f"in plan/api_spec.md:",
    ]
    for src, path in bad:
        lines.append(f"  - {src}: {path!r}")
    lines.append(f"known api_spec modules: {known_list}")
    lines.append(
        "fix: either retarget this task to one of the known modules "
        "(typical: src/<name>.py), or re-author plan/api_spec.md FIRST "
        "to include the module(s) this task needs. Note: the plan/ "
        "directory is the planner's own workspace — never a valid "
        "output_path for executor-side impl tasks."
    )
    raise AgoraError("\n".join(lines))


def _autofill_postcondition_args(
    name: str, args: dict[str, Any], output_path: str
) -> dict[str, Any]:
    """Backfill obvious postcondition args the model didn't pass.

    The 7B planner struggles with nested args dicts — it routinely passes
    ``{"name": "file_exists"}`` without the ``args: {rel: ...}`` dict,
    hitting ``TypeError: missing required argument 'rel'`` from the factory.
    Since the task already carries ``output_path`` and most code-file
    postconditions want exactly that value for ``rel``, the framework fills
    it in on the model's behalf. Explicit caller-supplied args are never
    overwritten.

    Only ``rel`` is autofilled today; other required args (like
    ``file_contains.substring``) are task-specific and can't be guessed.
    """
    if not output_path:
        return args
    from agora.plan.predicate_registry import _REGISTRY

    factory = _REGISTRY.get(name)
    if factory is None:
        return args  # let attach_postcondition surface the unknown-name error
    try:
        import inspect

        sig = inspect.signature(factory)
    except (TypeError, ValueError):
        return args
    params = sig.parameters
    if "rel" in params and "rel" not in args:
        # Only fill when rel has no default OR the task's output_path is
        # clearly more specific than the factory default (we're defensive and
        # always respect an explicit user value, but empty/missing → fill).
        args["rel"] = output_path
    return args


__all__ = ["PlanDraft"]
