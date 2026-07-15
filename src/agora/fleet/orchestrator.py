"""Fleet orchestrator.

Three operating modes:

1. :meth:`Orchestrator.single_task` — spawn or find one agent, execute one
   task, return the result. State machine is bypassed.

2. :meth:`Orchestrator.run_project` — drive a project through the full phase
   state machine (INIT → ANALYSIS → ARCHITECTURE → IMPLEMENTATION → TESTING →
   REVIEW → DONE|loop-back).

3. :meth:`Orchestrator.run_flow` — instantiate a :class:`~agora.core.flow.Flow`
   and then ``run_project``.

The orchestrator does not call the LLM or Matrix directly — it composes
:class:`~agora.fleet.dispatcher.Dispatcher`,
:class:`~agora.fleet.agent_runtime.AgentRuntime`,
:class:`~agora.matrix.room_manager.RoomManager`, and
:class:`~agora.matrix.sync.EventDispatcher`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agora.core.agent import AgentConfig, AgentIdentity
from agora.core.errors import AgoraError
from agora.core.flow import Flow, instantiate_flow
from agora.core.project import Project, transition_phase
from agora.core.task import Task, ready_tasks, transition_task
from agora.core.types import ProjectPhase, TaskStatus
from agora.fleet.agent_runtime import AgentRuntime, TaskResult
from agora.fleet.control import AbortedError
from agora.fleet.dispatcher import Dispatcher
from agora.fleet.inner_tools import ToolContext
from agora.fleet.llm_adapter import LLMProtocol
from agora.matrix.client import MatrixClientProtocol
from agora.matrix.events import PHASE_CHANGE_EVENT, phase_change_to_content
from agora.matrix.room_manager import RoomManager

logger = logging.getLogger(__name__)

LLMFactory = Callable[[str], LLMProtocol]
ReviewFn = Callable[[Project, list[TaskResult]], Awaitable["ReviewDecision"]]
VRAMGate = Callable[[str], Awaitable[None]]


@dataclass
class ReviewDecision:
    """Outcome of the REVIEW phase poll / human approval.

    ``approved=True`` advances the project to ``DONE``. ``approved=False``
    with ``return_to_phase`` set drives the loop-back path — the
    orchestrator re-opens the targeted phase, resets the relevant tasks
    to ``PENDING``, and resumes execution. ``feedback`` is surfaced to the
    next agent's prompt as a system-authored task comment.
    """

    approved: bool
    feedback: str = ""
    return_to_phase: ProjectPhase | None = None


@dataclass
class ProjectResult:
    """Aggregated outcome of one :meth:`Orchestrator.run_project` call.

    ``success`` is the conjunction of every task's postcondition outcome
    plus the review approval. ``task_results`` is in execution order;
    ``total_tokens`` aggregates input/output token counts (and ``cost_usd``
    if a metered backend sets it) across all agents.
    """

    project: Project
    success: bool
    task_results: list[TaskResult] = field(default_factory=list)
    total_tokens: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    project_room_id: str = ""


class Orchestrator:
    """Top-level run driver: phase state machine + agent dispatch + observer.

    The orchestrator does not call the LLM or Matrix directly. It composes
    :class:`~agora.fleet.dispatcher.Dispatcher` (agent assignment),
    :class:`~agora.fleet.agent_runtime.AgentRuntime` (per-task execution),
    :class:`~agora.matrix.room_manager.RoomManager` (room creation),
    and :class:`~agora.matrix.sync.EventDispatcher` (observer surface).

    Three entry points: :meth:`single_task` for a one-shot, :meth:`run_project`
    for a full phase-state-machine run, and :meth:`run_flow` to instantiate
    a declarative :class:`~agora.core.flow.Flow` and then ``run_project`` it.

    Per-project work directories live under ``work_dir/<project_name>/`` and
    are unified with the per-project git repo so auto-hook commits and the
    framework's view of artefacts share one tree.
    """

    def __init__(
        self,
        matrix_client: MatrixClientProtocol,
        room_manager: RoomManager,
        llm_factory: LLMFactory,
        work_dir: str,
        homeserver_name: str = "agora.local",
        max_parallel_agents: int = 3,
        vram_check: VRAMGate | None = None,
        *,
        enable_observer: bool = False,
        repo_root: str | None = None,
        knowledge_cache_dir: str | None = None,
        ollama_base_url: str,  # required config-shaped endpoint — no localhost default; inject from Settings.ollama_base_url
        skip_warmup: bool = False,
        warmup_deadline: float = 600.0,
        keep_alive: str = "30m",
        review_timeout_seconds: float = 300.0,  # keep in step with Settings.review_timeout_seconds
        enable_web_fetch: bool = False,
        fetch_timeout_seconds: float = 30.0,
        fetch_max_bytes: int = 1_048_576,
        fetch_max_text_bytes: int = 16_384,
        auto_hooks_enabled: bool = False,
        plan_authoring_enabled: bool = False,
        routed_retry_budget: int = 2,
        tool_errors: str = "raw",
        nudge_budget: int = 0,
        review_budget: int = 0,
        salvage_budget: int = 0,
        observer: Any = None,
    ) -> None:
        self._matrix = matrix_client
        self._rooms = room_manager
        self._llm_factory = llm_factory
        self._work_dir = work_dir
        self._homeserver_name = homeserver_name
        self._max_parallel = max(1, int(max_parallel_agents))
        self._vram_check = vram_check
        self._enable_observer = enable_observer
        self._repo_root = repo_root
        self._knowledge_cache_dir = knowledge_cache_dir
        self._ollama_base_url = ollama_base_url
        self._skip_warmup = skip_warmup
        self._warmup_deadline = warmup_deadline
        self._keep_alive = keep_alive
        self._review_timeout_seconds = review_timeout_seconds
        self._enable_web_fetch = enable_web_fetch
        self._fetch_timeout_seconds = fetch_timeout_seconds
        self._fetch_max_bytes = fetch_max_bytes
        self._fetch_max_text_bytes = fetch_max_text_bytes
        self._auto_hooks_enabled = auto_hooks_enabled
        # v3 harness-reliability knobs, threaded into each task's ToolContext.
        self._tool_errors = tool_errors
        self._nudge_budget = nudge_budget
        # v8 S6 completion-review budget (0 = off, byte-identical to v3.2).
        self._review_budget = review_budget
        # S7 reasoning-salvage budget (0 = off, construct-nothing).
        self._salvage_budget = salvage_budget
        # Gates the plan-authoring tool category (plan_upsert_agent,
        # plan_add_task_spec, plan_finalize). The plan-builder runner opts in;
        # every other runner leaves it False so emitted plans don't expose
        # meta-authoring tools to their architect role.
        self._plan_authoring_enabled = plan_authoring_enabled
        # v2.5: separate retry budget for router-driven upstream re-dispatches
        # (pytest ImportError detected in a test task → owning task retries).
        # Distinct from ``max_task_retries`` so routed retries don't eat the
        # owning task's normal in-phase retry pool.
        self._routed_retry_budget = max(0, int(routed_retry_budget))
        # Optional JSONL run observer (agora.observe.jsonl.RunObserver). When
        # None, no run.jsonl / tasks.jsonl is emitted — fully back-compatible.
        # Distinct from the Matrix sync observer gated by ``enable_observer``.
        self._observer = observer
        # Per-project cache shared across agents within a single run. Key = project_room.
        self._fetch_caches: dict[str, Any] = {}
        # Last control is exposed for MCP handlers that want to report pause/abort state.
        self._active_controls: dict[str, Any] = {}

    async def _preflight_models(self, agents: list[AgentConfig]) -> None:
        seen: set[str] = set()
        for cfg in agents:
            if cfg.model in seen:
                continue
            seen.add(cfg.model)
            if self._vram_check is not None:
                await self._vram_check(cfg.model)
            if cfg.model.startswith("ollama/") and not self._skip_warmup:
                from agora.fleet.vram import warmup

                await warmup(
                    cfg.model,
                    base_url=self._ollama_base_url,
                    deadline_seconds=self._warmup_deadline,
                    keep_alive=self._keep_alive,
                )

    def get_control(self, project_room_id: str) -> Any | None:
        """Return the live :class:`OrchestratorControl` for a given project room, if any."""
        return self._active_controls.get(project_room_id)

    async def _spin_flywheel(self, identities: list[AgentIdentity]) -> None:
        """Hydrate → decay → persist learnings for each agent before the run starts."""
        from agora.core.learning import decay_learnings, filter_active
        from agora.matrix.events import LEARNING_EVENT, learning_to_content

        for identity in identities:
            try:
                hydrated = await self._rooms.hydrate_identity(identity.room_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "flywheel hydrate failed for %s: %s", identity.config.name, exc
                )
                continue
            if not hydrated.learned_patterns:
                identity.learned_patterns = []
                continue
            decayed = decay_learnings(list(hydrated.learned_patterns))
            # Persist decay by posting updated learning events so the Matrix
            # timeline reflects current confidence.
            for learning in decayed:
                try:
                    await self._matrix.send_event(
                        identity.room_id, LEARNING_EVENT, learning_to_content(learning)
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("flywheel persist failed: %s", exc)
            # Inject only the active (above threshold) decayed learnings.
            identity.learned_patterns = filter_active(decayed)

    async def _reinforce_learnings(
        self, identity: AgentIdentity, ids: list[str]
    ) -> None:
        """Post reinforced copies of the active learnings for ``ids``."""
        if not ids:
            return
        from agora.core.learning import reinforce
        from agora.matrix.events import LEARNING_EVENT, learning_to_content

        by_id = {l.id: l for l in identity.learned_patterns}
        for lid in ids:
            learning = by_id.get(lid)
            if learning is None:
                continue
            boosted = reinforce(learning)
            try:
                await self._matrix.send_event(
                    identity.room_id, LEARNING_EVENT, learning_to_content(boosted)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("reinforce failed for %s: %s", lid, exc)

    def _maybe_route_upstream_error(
        self,
        task: Task,
        outcome: TaskResult,
        tasks: list[Task],
        routed_retries: dict[str, int],
    ) -> dict[str, Any] | None:
        """v2.5: if ``outcome`` contains a failed ``pytest_passes`` postcondition
        whose reason references an upstream file, return a routing descriptor
        naming the owning task. Returns None when nothing to route.

        The descriptor ``{"owning_task_id", "reason_excerpt", "feedback"}``
        carries the formatted feedback the caller should queue into
        ``control.task_comments[owning_task_id]`` for the retry's system prompt.
        """
        from agora.fleet.error_router import extract_failing_paths, find_owning_task

        if self._routed_retry_budget <= 0:
            return None
        # Find the failing pytest_passes postcondition result.
        pytest_failures: list[tuple[str, bool, str]] = [
            r for r in outcome.postcondition_results
            if r[0].startswith("pytest") and r[1] is False
        ]
        if not pytest_failures:
            return None
        reason = pytest_failures[0][2]
        candidates = extract_failing_paths(reason)
        if not candidates:
            return None
        # First path that resolves to a task other than ``task`` itself
        # and whose routed budget hasn't been spent.
        for path in candidates:
            owner = find_owning_task(path, tasks)
            if owner is None or owner.id == task.id:
                continue
            if routed_retries.get(owner.id, 0) >= self._routed_retry_budget:
                continue
            excerpt = reason.strip().splitlines()[:12]
            feedback = (
                "[SYSTEM] A downstream test task reported that your output "
                f"file triggered an import failure at test-collection time. "
                f"Specifically, {path!r} (which you own) imports a module "
                "that doesn't exist, or is missing a referenced name. Fix "
                "the import chain / create the missing module so downstream "
                "`from <your-package>.<your-module> import <name>` calls "
                "succeed. Full pytest output head:\n\n```\n"
                + "\n".join(excerpt)
                + "\n```"
            )
            return {
                "owning_task_id": owner.id,
                "owning_path": path,
                "feedback": feedback,
                "reason_excerpt": "\n".join(excerpt),
            }
        return None

    def _maybe_queue_narration_redirect(
        self, task: Task, outcome: TaskResult, control: Any
    ) -> None:
        """Detect 'model narrated its plan instead of calling a tool' and queue
        a strong redirect into the task's next-attempt system prompt.

        Observed in Run 15 (FastAPI CRUD): build_create and build_update both
        returned summaries like *"Let's read app.py to find an appropriate
        anchor"* or *"we can see that the if __name__ check appears..."* —
        descriptions of plans, not tool calls. The task's expected
        ``output_path`` was never written, so ``artifacts`` missed it and the
        file-exists postcondition failed cleanly.

        When we detect that shape, we push a system-authored comment into
        ``control.task_comments[task.id]`` so the next retry's
        ``_compose_system_prompt`` injects it above the normal instructions.
        The comment is intentionally loud: the model needs a firm push to
        break out of the narration loop.
        """
        if control is None or not hasattr(control, "task_comments"):
            return
        if not task.output_path:
            return
        written = _output_path_was_produced(task.output_path, outcome.artifacts)
        if written:
            return

        redirect = (
            "[SYSTEM] Your PREVIOUS attempt narrated a plan instead of calling "
            "the tool. Do NOT describe what you're about to do. Do NOT re-read "
            "files — they are already provided in the user message. Your FIRST "
            "emission on this turn MUST be a tool_use block (edit_file_replace, "
            "edit_file_insert_before, edit_file_append, or write_file). If you "
            "find yourself saying 'let's' or 'we can see' or 'an appropriate "
            "anchor', STOP — just invoke the tool with the literal arguments "
            f"from the task instruction. Expected output path: "
            f"{task.output_path!r}."
        )
        existing = control.task_comments.setdefault(task.id, [])
        # Avoid stacking duplicates across multiple failed attempts.
        if redirect not in existing:
            existing.append(redirect)
        logger.info(
            "narration redirect queued for task %s (expected output %r not written)",
            task.id, task.output_path,
        )

    async def _record_failure_learnings(
        self, identity: AgentIdentity, task: Task, outcome: TaskResult
    ) -> None:
        """Synthesize learnings from every failed postcondition on a task.

        Runs regardless of whether the agent called ``report_learning`` — the
        postcondition contract is itself the ground truth. Learnings are posted
        to Matrix *and* pushed into the agent's in-memory ``learned_patterns``
        so a same-run loopback retry sees them without waiting for the next
        flywheel spin.

        Duplicate failures (same task + predicate + normalised reason) produce
        the same stable id, so re-posting reinforces rather than duplicating.
        """
        from agora.core.learning import reinforce
        from agora.fleet.auto_learning import synthesize_failure_learning
        from agora.matrix.events import LEARNING_EVENT, learning_to_content

        existing = {l.id: l for l in identity.learned_patterns}
        for predicate_name, passed, reason in outcome.postcondition_results:
            if passed:
                continue
            learning = synthesize_failure_learning(
                task_id=task.id, predicate_name=predicate_name, reason=reason
            )
            if learning.id in existing:
                learning = reinforce(existing[learning.id])
            try:
                await self._matrix.send_event(
                    identity.room_id, LEARNING_EVENT, learning_to_content(learning)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "auto-learning persist failed for %s: %s", learning.id, exc
                )
                continue
            # Update in-memory view so the next task sees the learning without
            # re-hydrating from Matrix.
            identity.learned_patterns = [
                *(l for l in identity.learned_patterns if l.id != learning.id),
                learning,
            ]
            existing[learning.id] = learning
            logger.info(
                "auto-learning recorded: task=%s predicate=%s id=%s",
                task.id, predicate_name, learning.id,
            )

    # ---------------------------------------------------------------- single task

    async def single_task(
        self,
        agent_config: AgentConfig,
        task: Task,
        project_room_id: str | None = None,
    ) -> TaskResult:
        """Spawn one agent, execute one task, return the result."""
        await self._preflight_models([agent_config])
        room_id, agent_id = await self._rooms.create_identity_room(agent_config)
        knowledge_refs = await self._upload_agent_knowledge(room_id, agent_config)
        identity = AgentIdentity(
            agent_id=agent_id,
            room_id=room_id,
            config=agent_config,
            knowledge_refs=knowledge_refs,
        )
        project_room = project_room_id or await self._rooms.create_project_room(
            project_name=f"single-{task.id[:8]}",
            agent_ids=[agent_id],
        )
        single_name = f"single-{task.id[:8]}"
        project_work_dir = self._project_work_dir(single_name)
        repo = self._make_repo_manager(single_name)
        runtime = self._make_runtime(
            identity, project_room, repo=repo, project_work_dir=project_work_dir
        )
        return await runtime.execute_task(task, identity)

    # --------------------------------------------------------------- full project

    async def run_project(
        self,
        name: str,
        agents: list[AgentConfig],
        tasks: list[Task],
        review_fn: ReviewFn | None = None,
        max_loopbacks: int = 2,
        staged_tasks: dict[str, Any] | None = None,
        max_task_retries: int = 0,
    ) -> ProjectResult:
        """Run a project through the state machine.

        ``review_fn`` is called during REVIEW; the default auto-approves if all
        task postconditions passed. It may return a ``return_to_phase`` to
        trigger a loop-back.

        ``staged_tasks`` maps ``task.id`` →
        :class:`~agora.fleet.stage_runner.StagedTask`. Any task whose id is in
        the map is routed through the :class:`StageRunner` instead of the
        one-shot :meth:`AgentRuntime.execute_task` loop.

        ``max_task_retries`` (default 0) is the number of *in-phase* retries a
        task is granted after a failed attempt. 0 preserves legacy behaviour
        (a single attempt, then the cross-phase ``max_loopbacks`` machinery
        takes over). With ``max_task_retries=2`` each task gets up to 3 total
        attempts within the same phase; between attempts, auto-learnings and
        the narration redirect are injected so the retry sees the feedback
        immediately rather than waiting for the next phase boundary.
        """
        started = datetime.now(UTC)

        await self._preflight_models(agents)

        # --- INIT: spawn identity rooms, upload knowledge, open project room ---
        identities: list[AgentIdentity] = []
        for config in agents:
            room_id, agent_id = await self._rooms.create_identity_room(config)
            knowledge_refs = await self._upload_agent_knowledge(room_id, config)
            identities.append(
                AgentIdentity(
                    agent_id=agent_id,
                    room_id=room_id,
                    config=config,
                    knowledge_refs=knowledge_refs,
                )
            )
        project_room = await self._rooms.create_project_room(
            project_name=name, agent_ids=[i.agent_id for i in identities]
        )

        # --- Flywheel: hydrate prior learnings, apply decay, persist, inject ---
        await self._spin_flywheel(identities)

        # --- Per-project work dir + repo (unified: same directory) ---
        project_work_dir = self._project_work_dir(name)
        repo = self._make_repo_manager(name)

        project = Project(
            id=str(uuid.uuid4()),
            name=name,
            agents=tuple(i.agent_id for i in identities),
            tasks=tuple(tasks),
            created_at=started.isoformat(),
            git_repo_path=str(repo.repo_path) if repo is not None else "",
        )
        dispatcher = Dispatcher(identities)

        # --- Observer layer: SyncService + Renderer + ReviewCoordinator + control ---
        control, sync_service, resolved_review_fn = await self._start_observer(
            project_room_id=project_room,
            identity_rooms=[i.room_id for i in identities],
            user_review_fn=review_fn,
            project_work_dir=project_work_dir,
        )

        all_results: list[TaskResult] = []
        total_tokens = {"input_tokens": 0, "output_tokens": 0}
        loopbacks = 0

        phase_order: tuple[ProjectPhase, ...] = (
            ProjectPhase.ANALYSIS,
            ProjectPhase.ARCHITECTURE,
            ProjectPhase.IMPLEMENTATION,
            ProjectPhase.TESTING,
            ProjectPhase.REVIEW,
        )

        index = 0
        project = await self._advance_phase(
            project, ProjectPhase.ANALYSIS, "project kickoff", project_room
        )

        try:
            while True:
                current = phase_order[index]
                try:
                    phase_results = await self._run_phase(
                        project,
                        current,
                        dispatcher,
                        project_room,
                        control=control,
                        repo=repo,
                        identities=identities,
                        project_work_dir=project_work_dir,
                        staged_tasks=staged_tasks,
                        max_task_retries=max_task_retries,
                    )
                except AbortedError as exc:
                    project = await self._advance_phase(
                        project,
                        ProjectPhase.FAILED,
                        f"aborted by observer: {exc}",
                        project_room,
                    )
                    break
                all_results.extend(phase_results)
                for r in phase_results:
                    for k, v in r.token_usage.items():
                        total_tokens[k] = total_tokens.get(k, 0) + int(v)

                project = self._apply_phase_results(project, phase_results)

                if current == ProjectPhase.REVIEW:
                    decision = await resolved_review_fn(project, all_results)
                    if decision.approved:
                        project = await self._advance_phase(
                            project, ProjectPhase.DONE, "approved", project_room
                        )
                        break
                    if loopbacks >= max_loopbacks or decision.return_to_phase is None:
                        project = await self._advance_phase(
                            project,
                            ProjectPhase.FAILED,
                            f"review rejected (loopback limit): {decision.feedback}",
                            project_room,
                        )
                        break
                    loopbacks += 1
                    target = decision.return_to_phase
                    project = await self._advance_phase(
                        project, target, f"review loop-back: {decision.feedback}", project_room
                    )
                    project = _reset_tasks_for_retry(project)
                    index = phase_order.index(target)
                    continue

                # TESTING → IMPLEMENTATION loop-back if any postcondition fails.
                if current == ProjectPhase.TESTING and any(not r.success for r in phase_results):
                    if loopbacks < max_loopbacks:
                        loopbacks += 1
                        project = await self._advance_phase(
                            project,
                            ProjectPhase.IMPLEMENTATION,
                            "tests failed: loop back",
                            project_room,
                        )
                        project = _reset_tasks_for_retry(project)
                        index = phase_order.index(ProjectPhase.IMPLEMENTATION)
                        continue

                index += 1
                next_phase = phase_order[index]
                project = await self._advance_phase(
                    project, next_phase, f"entering {next_phase.value}", project_room
                )
        finally:
            # Always stop the sync service + unregister the control.
            if sync_service is not None:
                try:
                    await sync_service.stop()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("sync service stop raised: %s", exc)
            self._active_controls.pop(project_room, None)

        ended = datetime.now(UTC)
        result = ProjectResult(
            project=project,
            success=project.phase == ProjectPhase.DONE,
            task_results=all_results,
            total_tokens=total_tokens,
            duration_seconds=(ended - started).total_seconds(),
            project_room_id=project_room,
        )
        if self._observer is not None:
            self._emit_run_observations(result, agents, ended)
        return result

    def _emit_run_observations(
        self,
        result: ProjectResult,
        agents: list[AgentConfig],
        ended: datetime,
    ) -> None:
        """Flush one TaskRecord per task then the RunRecord (after tasks).

        Best-effort: a malformed/failing emit must never break a run that
        otherwise completed. The observer owns file handles and schema
        validation.
        """
        observer = self._observer
        try:
            role_of = {a.name: a.role.value for a in agents}
            # Last result wins for tasks that ran more than once (loop-backs).
            results_by_id: dict[str, TaskResult] = {}
            for r in result.task_results:
                results_by_id[r.task_id] = r
            tasks_passed = tasks_failed = tasks_first_pass = 0
            for idx, task in enumerate(result.project.tasks):
                tr = results_by_id.get(task.id)
                role = role_of.get(task.agent_id or "", "")
                record = observer.record_task_from_result(
                    task=task, result=tr, role=role, task_index=idx
                )
                if record.status == "passed":
                    tasks_passed += 1
                elif record.status in ("failed", "error"):
                    tasks_failed += 1
                if record.first_pass:
                    tasks_first_pass += 1
            observer.record_run(
                duration_s=result.duration_seconds,
                success=result.success,
                exit_code=0 if result.success else 1,
                tasks_total=len(result.project.tasks),
                tasks_passed=tasks_passed,
                tasks_failed=tasks_failed,
                tasks_first_pass=tasks_first_pass,
                model_offloaded=None,
                tokens_in=int(result.total_tokens.get("input_tokens", 0)),
                tokens_out=int(result.total_tokens.get("output_tokens", 0)),
                ended_at=ended.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 — observability must not fail a run
            logger.warning("run observation emit failed: %s", exc)
        finally:
            try:
                observer.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("observer.close raised: %s", exc)

    async def run_flow(self, flow: Flow, project_name: str) -> ProjectResult:
        """Materialise a declarative :class:`~agora.core.flow.Flow` then run it.

        Equivalent to ``instantiate_flow(flow, project_name)`` followed by
        :meth:`run_project`. Use this when driving Agora from a YAML plan
        file (``flows/*.plan.yaml``); use ``run_project`` directly when
        agents and tasks are constructed in code.
        """
        agents, tasks = instantiate_flow(flow, project_name)
        return await self.run_project(project_name, agents, tasks)

    # --------------------------------------------------------------- internals

    async def _run_phase(
        self,
        project: Project,
        phase: ProjectPhase,
        dispatcher: Dispatcher,
        project_room: str,
        *,
        control: Any = None,
        repo: Any = None,
        identities: list[AgentIdentity] | None = None,
        project_work_dir: str | None = None,
        staged_tasks: dict[str, Any] | None = None,
        max_task_retries: int = 0,
    ) -> list[TaskResult]:
        """Execute all ready tasks for this phase concurrently up to max_parallel.

        Between each ``gather`` wave, waits on the observer's pause gate and
        short-circuits with :class:`AbortedError` if the observer aborted.

        ``max_task_retries`` controls in-phase auto-retry: when a task fails,
        its status is flipped back to PENDING so the next wave of the same
        phase re-runs it — with the auto-learnings + narration redirect from
        the prior attempt already injected. Retries preserve cumulative token
        usage and iteration counts in the returned :class:`TaskResult`.
        """
        from dataclasses import replace as _dc_replace

        # Dict keyed by task.id so retried tasks overwrite rather than
        # accumulating duplicate result rows.
        results_by_task: dict[str, TaskResult] = {}
        attempts: dict[str, int] = {}
        # v2.5 error-routing state (scoped per phase). ``routed_retries``
        # counts how many times the router has re-dispatched a given task
        # as an "owner" (cap at ``self._routed_retry_budget``).
        # ``pending_verification`` maps test_task_id → owning_task_id so we
        # re-dispatch the verifier after the owner finishes successfully.
        routed_retries: dict[str, int] = {}
        pending_verification: dict[str, str] = {}
        pending = list(project.tasks)
        semaphore = asyncio.Semaphore(self._max_parallel)

        async def _execute(
            task: Task,
        ) -> tuple[Task, AgentIdentity | None, TaskResult | Exception]:
            async with semaphore:
                try:
                    agent = dispatcher.assign(task)
                except AgoraError as exc:
                    return task, None, exc
                logger.info("dispatch: task %s -> agent %s", task.id, agent.config.name)
                if self._observer is not None:
                    try:
                        self._observer.task_started(task.id)
                    except Exception as exc:  # noqa: BLE001 — observability only
                        logger.debug("observer.task_started raised: %s", exc)
                runtime = self._make_runtime(
                    agent,
                    project_room,
                    control=control,
                    repo=repo,
                    project_work_dir=project_work_dir,
                )
                staged = (staged_tasks or {}).get(task.id)
                started_at = time.monotonic()
                try:
                    if staged is not None:
                        from agora.fleet.stage_runner import StageRunner

                        runner = StageRunner(runtime)
                        outcome = await runner.execute_staged_task(staged, agent)
                    else:
                        outcome = await runtime.execute_task(task, agent)
                    from dataclasses import replace as _dc_replace

                    outcome = _dc_replace(
                        outcome, duration_s=time.monotonic() - started_at
                    )
                    logger.info(
                        "task %s done: success=%s iterations=%d",
                        task.id, outcome.success, outcome.iterations,
                    )
                    return task, agent, outcome
                except Exception as exc:  # noqa: BLE001 — surface as task failure
                    return task, agent, exc
                finally:
                    dispatcher.release(agent)

        while True:
            if control is not None:
                await control.wait_unpaused()
                control.raise_if_aborted()
            ready = ready_tasks(pending)
            if not ready:
                break
            # AGORA_SERIAL_TASKS (debug, default off): dispatch ready tasks
            # sequentially instead of concurrently, to isolate scheduling/batching
            # as a non-determinism source. Off ⇒ the concurrent gather, unchanged.
            # Registered debug-flag (integration-hardening 2B.3 allowlist): env-only.
            if os.getenv("AGORA_SERIAL_TASKS", "").strip().lower() in ("1", "true", "yes", "on"):
                outcomes = [await _execute(t) for t in ready]
            else:
                outcomes = await asyncio.gather(*(_execute(t) for t in ready))
            for task, agent, outcome in outcomes:
                attempts[task.id] = attempts.get(task.id, 0) + 1
                attempt_n = attempts[task.id]
                budget = max_task_retries + 1

                if isinstance(outcome, Exception):
                    logger.error(
                        "task %s raised (attempt %d/%d): %s",
                        task.id, attempt_n, budget, outcome,
                    )
                    err_result = TaskResult(
                        task_id=task.id,
                        success=False,
                        output=f"ERROR: {type(outcome).__name__}: {outcome}",
                    )
                    can_retry = attempt_n < budget
                    # Merge prior attempt's usage/iterations so we don't lose it.
                    prior = results_by_task.get(task.id)
                    if prior is not None:
                        err_result = _dc_replace(
                            err_result,
                            token_usage=_sum_usage(prior.token_usage, err_result.token_usage),
                            iterations=prior.iterations + err_result.iterations,
                        )
                    results_by_task[task.id] = err_result
                    pending = _replace_task(
                        pending,
                        task.id,
                        status=TaskStatus.PENDING if can_retry else TaskStatus.FAILED,
                        summary=str(outcome),
                    )
                    if can_retry:
                        logger.info(
                            "task %s will auto-retry (attempt %d/%d)",
                            task.id, attempt_n + 1, budget,
                        )
                    continue

                # Accumulate token usage + iterations across attempts for visibility.
                prior = results_by_task.get(task.id)
                if prior is not None:
                    outcome = _dc_replace(
                        outcome,
                        token_usage=_sum_usage(prior.token_usage, outcome.token_usage),
                        iterations=prior.iterations + outcome.iterations,
                    )
                results_by_task[task.id] = outcome

                if outcome.success:
                    pending = _replace_task(
                        pending,
                        task.id,
                        status=TaskStatus.DONE,
                        summary=outcome.output[:500],
                    )
                    if agent is not None and outcome.reinforced_ids:
                        await self._reinforce_learnings(agent, outcome.reinforced_ids)
                    # v2.5: if other tasks were waiting on THIS task's retry
                    # (via the error router), flip them back to PENDING so they
                    # re-verify now that the upstream fix is in place.
                    verifiers = [
                        tid for tid, oid in pending_verification.items()
                        if oid == task.id
                    ]
                    for tid in verifiers:
                        pending_verification.pop(tid, None)
                        pending = _replace_task(
                            pending,
                            tid,
                            status=TaskStatus.PENDING,
                            summary=f"verifying after owner {task.id!r} retry",
                        )
                        logger.info(
                            "re-dispatching pending-verification task=%s after owner %s succeeded",
                            tid, task.id,
                        )
                    continue

                # Failed outcome — record learnings + redirect unconditionally.
                # Dedup is handled by the stable-hash id in synthesize_failure_learning
                # and the set-based dedup inside _maybe_queue_narration_redirect, so
                # running these on every failed attempt reinforces rather than bloats.
                if agent is not None:
                    await self._record_failure_learnings(agent, task, outcome)
                    self._maybe_queue_narration_redirect(task, outcome, control)

                # v2.5: scope-bounded error routing. If the failure is a
                # pytest ImportError / ModuleNotFoundError pointing at a file
                # owned by a DIFFERENT task, return it to that owner with
                # structural feedback and soft-pass this task's postcondition.
                # Verification re-runs once the owner finishes.
                routed = self._maybe_route_upstream_error(
                    task, outcome, pending, routed_retries
                )
                if routed is not None and control is not None and hasattr(
                    control, "task_comments"
                ):
                    owner_id = routed["owning_task_id"]
                    control.task_comments.setdefault(owner_id, []).append(
                        routed["feedback"]
                    )
                    # Flip owner back to PENDING for re-dispatch with feedback.
                    pending = _replace_task(
                        pending,
                        owner_id,
                        status=TaskStatus.PENDING,
                        summary=(
                            f"routed retry {routed_retries.get(owner_id, 0) + 1}"
                        ),
                    )
                    # This task's pytest failure is soft-passed: the tester did
                    # its job (diagnosed an upstream bug); mark task DONE and
                    # queue for re-verification once the owner completes.
                    pending = _replace_task(
                        pending,
                        task.id,
                        status=TaskStatus.DONE,
                        summary=f"routed to {owner_id}",
                    )
                    pending_verification[task.id] = owner_id
                    routed_retries[owner_id] = (
                        routed_retries.get(owner_id, 0) + 1
                    )
                    # Reset attempt counter so verification re-run gets a
                    # fresh in-phase budget (independent of this attempt).
                    attempts[task.id] = 0
                    # Stash a soft-passed outcome so result reporting reflects
                    # the routing rather than the raw pytest failure.
                    results_by_task[task.id] = _dc_replace(
                        outcome,
                        success=True,
                        postcondition_results=[
                            (
                                name,
                                True if name.startswith("pytest") else passed,
                                (
                                    f"routed to task {owner_id!r}"
                                    if name.startswith("pytest") and not passed
                                    else reason
                                ),
                            )
                            for name, passed, reason in outcome.postcondition_results
                        ],
                    )
                    logger.info(
                        "routing pytest error from task=%s upstream to task=%s (routed retry %d/%d)",
                        task.id, owner_id,
                        routed_retries[owner_id], self._routed_retry_budget,
                    )
                    project = _with_tasks(project, pending)
                    continue

                can_retry = attempt_n < budget
                pending = _replace_task(
                    pending,
                    task.id,
                    status=TaskStatus.PENDING if can_retry else TaskStatus.FAILED,
                    summary=outcome.output[:500],
                )
                if can_retry:
                    logger.info(
                        "task %s failed (attempt %d/%d): auto-retrying with injected learnings",
                        task.id, attempt_n, budget,
                    )
            project = _with_tasks(project, pending)
        return list(results_by_task.values())

    def _make_runtime(
        self,
        identity: AgentIdentity,
        project_room: str,
        *,
        control: Any = None,
        repo: Any = None,
        project_work_dir: str | None = None,
    ) -> AgentRuntime:
        from agora.fleet.distiller import make_distill_fn

        fetcher = self._make_knowledge_fetcher() if identity.knowledge_refs else None
        url_fetcher = self._make_url_fetcher(project_room) if self._enable_web_fetch else None
        llm = self._llm_factory(identity.config.model)
        # Distill large read_file results so design/impl tasks don't blow up
        # num_ctx when reading kb artefacts. task_focus is set per-task in
        # AgentRuntime.execute_task / StageRunner.execute_staged_task.
        distill_fn = make_distill_fn(llm, model=identity.config.model)
        ctx = ToolContext(
            work_dir=project_work_dir or self._work_dir,
            matrix_client=self._matrix,
            agent_room_id=identity.room_id,
            project_room_id=project_room,
            git_repo=repo,
            knowledge_refs=list(identity.knowledge_refs),
            knowledge_fetcher=fetcher,
            fetch_fn=url_fetcher,
            control=control,
            auto_hooks_enabled=self._auto_hooks_enabled,
            plan_authoring_enabled=self._plan_authoring_enabled,
            tool_errors=self._tool_errors,
            nudge_budget=self._nudge_budget,
            review_budget=self._review_budget,
            salvage_budget=self._salvage_budget,
            distill_fn=distill_fn,
        )
        return AgentRuntime(llm=llm, matrix_client=self._matrix, tool_context=ctx)

    # ------------------------------------------------------------- Sprint 7 helpers

    def _project_work_dir(self, project_name: str) -> str:
        """Return (and create) ``<work_dir>/<safe(project_name)>``.

        This is both the directory agents write files into *and* the git working
        tree for the project — keeping them identical is what makes ``git_commit``
        actually persist per-task work.
        """
        from pathlib import Path

        path = Path(self._work_dir) / _safe_dir(project_name)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _make_repo_manager(self, project_name: str) -> Any | None:
        """Open or init the per-project git repo.

        The repo lives at the project's work_dir so agent file writes and git
        commits operate on the same tree. ``repo_root`` is accepted for
        backwards compatibility but ignored — work_dir is authoritative.
        """
        from agora.git.repo_manager import RepoManager

        path = self._project_work_dir(project_name)
        repo = RepoManager(path)
        if repo._repo is None:  # type: ignore[attr-defined] — private flag check
            repo.init_project_repo(project_name)
        return repo

    async def _upload_agent_knowledge(
        self, room_id: str, config: AgentConfig
    ) -> list[str]:
        """Upload every local knowledge file referenced by an AgentConfig."""
        refs: list[str] = []
        for file_path in config.knowledge_files:
            try:
                mxc = await self._rooms.upload_knowledge(room_id, file_path)
                refs.append(mxc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "knowledge upload failed for %s (%s): %s",
                    config.name,
                    file_path,
                    exc,
                )
        return refs

    def _make_url_fetcher(self, project_room_id: str):
        """Return an ``async (url) -> str`` fetcher that shares a per-project cache."""
        from agora.fleet.web_fetch import FetchCache, make_fetcher

        cache = self._fetch_caches.setdefault(project_room_id, FetchCache())
        return make_fetcher(
            cache=cache,
            timeout_seconds=self._fetch_timeout_seconds,
            max_bytes=self._fetch_max_bytes,
            max_text_bytes=self._fetch_max_text_bytes,
        )

    def _make_knowledge_fetcher(self):
        """Return a coroutine that downloads an MXC URI into the cache dir."""
        if not self._knowledge_cache_dir:
            return None
        cache_dir = self._knowledge_cache_dir

        async def _fetch(mxc_uri: str) -> str:
            return await self._matrix.download_file(mxc_uri, cache_dir)

        return _fetch

    async def _start_observer(
        self,
        *,
        project_room_id: str,
        identity_rooms: list[str],
        user_review_fn: ReviewFn | None,
        project_work_dir: str | None = None,
    ) -> tuple[Any | None, Any | None, ReviewFn]:
        """Start the per-project observer stack (if enabled). Returns (control, service, review_fn)."""
        # Build a control object even when the observer is disabled so pause/abort
        # hooks in _run_phase remain uniform (they just never fire).
        from agora.fleet.control import OrchestratorControl

        control = OrchestratorControl(
            project_room_id=project_room_id, matrix_client=self._matrix
        )
        self._active_controls[project_room_id] = control

        if not self._enable_observer:
            return control, None, (user_review_fn or _default_auto_review)

        from agora.matrix.sync import EventDispatcher
        from agora.observe.renderer import Renderer
        from agora.observe.review import ReviewCoordinator
        from agora.observe.sync_service import SyncService

        dispatcher = EventDispatcher()
        renderer = Renderer(self._matrix, dispatcher)
        renderer.attach()
        dispatcher.on_command(control.handle_command)
        dispatcher.on_reaction(control.handle_reaction)
        dispatcher.on_reply(control.handle_reply)

        coordinator = ReviewCoordinator(
            matrix_client=self._matrix,
            dispatcher=dispatcher,
            project_room_id=project_room_id,
            timeout_seconds=self._review_timeout_seconds,
            project_work_dir=project_work_dir,
            control=control,
        )
        coordinator.attach()

        # Decision-poll router: a second poll-response handler that
        # cross-references the control's poll_event_to_decision map and
        # resolves the right Future when the user clicks. Review-phase polls
        # are not in that map so they fall through to the coordinator above.
        async def _route_decision_response(room_id, response):
            if room_id != project_room_id:
                return
            decision_id = control.decision_id_for_poll(response.poll_event_id)
            if decision_id is None:
                return
            resolved = control.resolve_decision(decision_id, response.answer_id)
            if resolved:
                logger.info(
                    "decision resolved: decision_id=%s answer=%s",
                    decision_id, response.answer_id,
                )

        dispatcher.on_poll_response(_route_decision_response)

        watched = [project_room_id, *identity_rooms]
        service = SyncService(self._matrix, dispatcher, rooms=watched)

        # Post the observer cheat sheet so reviewers can discover the
        # interactive surface (reactions, replies, /agora commands) without
        # typing /agora help. Best-effort — failure here must not stop the run.
        try:
            from agora.observe.formatters import format_command_reference

            await self._matrix.send_event(
                project_room_id, "m.room.message", format_command_reference().to_content()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("command-reference card post failed: %s", exc)

        try:
            await service.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sync service failed to start (%s); falling back to headless", exc
            )
            return control, None, (user_review_fn or _default_auto_review)

        resolved_review_fn = user_review_fn or coordinator.request_review
        return control, service, resolved_review_fn

    async def _advance_phase(
        self,
        project: Project,
        new_phase: ProjectPhase,
        reason: str,
        project_room: str,
    ) -> Project:
        logger.info("advance_phase: %s -> %s (%s)", project.phase.value, new_phase.value, reason)
        updated = transition_phase(project, new_phase, reason)
        change = updated.phase_history[-1]
        await self._matrix.send_event(
            project_room, PHASE_CHANGE_EVENT, phase_change_to_content(change)
        )
        return updated

    @staticmethod
    def _apply_phase_results(project: Project, results: list[TaskResult]) -> Project:
        by_id = {r.task_id: r for r in results}
        new_tasks = []
        for t in project.tasks:
            r = by_id.get(t.id)
            if r is None:
                new_tasks.append(t)
                continue
            status = TaskStatus.DONE if r.success else TaskStatus.FAILED
            from dataclasses import replace as _replace

            new_tasks.append(
                _replace(
                    t,
                    status=status,
                    artifacts=tuple(r.artifacts),
                    result_summary=r.output[:500],
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
        return _with_tasks(project, new_tasks)


# ------------------------------ small free helpers ------------------------------


def _sum_usage(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = out.get(k, 0) + int(v)
    return out


def _with_tasks(project: Project, tasks: list[Task]) -> Project:
    return Project(
        id=project.id,
        name=project.name,
        phase=project.phase,
        agents=project.agents,
        tasks=tuple(tasks),
        phase_history=project.phase_history,
        git_repo_path=project.git_repo_path,
        created_at=project.created_at,
    )


def _replace_task(
    tasks: list[Task], task_id: str, status: TaskStatus, summary: str
) -> list[Task]:
    from dataclasses import replace as _replace

    out: list[Task] = []
    for t in tasks:
        if t.id != task_id:
            out.append(t)
            continue
        out.append(
            _replace(
                t,
                status=status,
                result_summary=summary,
                updated_at=datetime.now(UTC).isoformat(),
            )
        )
    return out


def _reset_tasks_for_retry(project: Project) -> Project:
    """Reset tasks that failed back to PENDING so a loop-back phase can re-run them."""
    new_tasks: list[Task] = []
    for t in project.tasks:
        if t.status == TaskStatus.FAILED:
            try:
                new_tasks.append(transition_task(t, TaskStatus.PENDING))
            except AgoraError:
                new_tasks.append(t)
        else:
            new_tasks.append(t)
    return _with_tasks(project, new_tasks)


def _output_path_was_produced(
    output_path: str, artifacts: list[str] | tuple[str, ...]
) -> bool:
    """Was the task's declared output_path among the artifacts it produced?

    Both dicts and strings are compared by substring match so
    ``artifacts=['bot.py', 'README.md']`` matches ``output_path='bot.py'``
    whether the artifact is tracked as a bare rel path or a fully-qualified
    one.
    """
    if not output_path:
        return False
    for art in artifacts or ():
        if not isinstance(art, str):
            continue
        if output_path == art or output_path in art or art in output_path:
            return True
    return False


def _safe_dir(name: str) -> str:
    """Make a project name safe to use as a directory component."""
    import re

    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "project"


async def _default_auto_review(
    project: Project, results: list[TaskResult]
) -> ReviewDecision:
    failed = [r for r in results if not r.success]
    if not failed:
        return ReviewDecision(approved=True)
    return ReviewDecision(
        approved=False,
        feedback=f"{len(failed)} task(s) failed postconditions",
        return_to_phase=ProjectPhase.IMPLEMENTATION,
    )
