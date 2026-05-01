"""Micro-stage task execution.

A :class:`StagedTask` splits one logical task into a sequence of focused LLM
calls. Each :class:`Stage` gets its own fresh message history, a small
``max_iterations`` cap, and only the files it needs pre-loaded into the user
message. The task's postconditions are evaluated once at the end over whatever
the cumulative stages produced.

Two wins for weak models:
  1. Fresh context per stage — no 37-message accumulation blowing past num_ctx.
  2. One action per stage — the model decides less, hallucinates less.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#: Digit-emoji keys used for decision-stage reaction voting. Each option in a
#: decision stage gets the emoji at the matching index. Element may strip the
#: variation-selector-16 (\ufe0f), so :meth:`OrchestratorControl.handle_reaction`
#: matches both forms.
_EMOJI_DIGITS: tuple[str, ...] = (
    "1\ufe0f\u20e3",
    "2\ufe0f\u20e3",
    "3\ufe0f\u20e3",
    "4\ufe0f\u20e3",
    "5\ufe0f\u20e3",
    "6\ufe0f\u20e3",
    "7\ufe0f\u20e3",
    "8\ufe0f\u20e3",
    "9\ufe0f\u20e3",
)

from agora.core.agent import AgentIdentity
from agora.core.task import Task
from agora.fleet.agent_runtime import (
    AgentRuntime,
    TaskResult,
    _collect_artifacts,
    _evaluate_postconditions,
    _merge_usage,
    build_output_path_banner,
)
from agora.fleet.inner_tools import ToolContext, get_tool_definitions, get_tool_executor

logger = logging.getLogger(__name__)


def _output_path_has_content(ctx: ToolContext) -> bool:
    """Return True if write_file should be hidden from the LLM manifest.

    Two conditions trigger the hide:
      1. The task's expected output file already has bytes on disk.
      2. ``ctx.write_file_blocked`` is True — flipped by write_file's own
         overwrite guard when the model tries to clobber a non-empty file
         that ISN'T the task's output_path (e.g. README, requirements.txt
         auto-created at project init). One block → hide for the task's
         remaining turns; the 7B otherwise keeps cycling on the same error.

    Non-existence or an unreadable path falls through to the flag check.
    """
    # Short-circuit on the explicit per-task flag first.
    if getattr(ctx, "write_file_blocked", False):
        return True
    rel = getattr(ctx, "expected_output_path", "") or ""
    work_dir = getattr(ctx, "work_dir", "") or ""
    if not rel or not work_dir:
        return False
    try:
        path = Path(work_dir) / rel
        return path.is_file() and path.stat().st_size > 0
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class Stage:
    """One focused LLM call in a staged task — OR a declarative decision stage.

    When ``kind == "llm"`` (the default), ``instruction`` + ``context_files``
    feed a user-message into the agent's tool-use loop bounded by
    ``max_iterations``. When ``kind == "decision"``, ``decision_id`` /
    ``question`` / ``options`` / ``output_path`` describe a Matrix poll +
    reaction card the framework posts on the LLM's behalf; no LLM call is
    made. The dispatch happens in ``StageRunner.execute_staged_task``.
    """

    instruction: str = ""
    """The user-message for this stage. Should describe ONE action. Empty
    when ``kind='decision'``."""

    context_files: tuple[str, ...] = ()
    """Files (relative to work_dir) to include verbatim in the user message."""

    max_iterations: int = 5
    """Per-stage iteration cap — much smaller than a normal task's 20."""

    validation: Callable[[ToolContext], tuple[bool, str]] | None = None
    """Optional check run after the stage completes. Returns ``(ok, reason)``."""

    name: str = ""
    """Optional identifier for logging."""

    # v2.1 additions — decision-stage fields. Ignored when ``kind == "llm"``.
    kind: str = "llm"
    decision_id: str = ""
    question: str = ""
    options: tuple[str, ...] = ()
    output_path: str = ""

    # v2.3 addition — typed args for framework validation stages (e.g. the
    # plan_validate_agent stage reads ``expected_name`` / ``expected_role``
    # out of this dict). Not used by ``llm`` or ``decision`` stages.
    validation_args: dict[str, str] = field(default_factory=dict)

    # v2.3 addition — per-stage tool-manifest filter for ``kind="llm"`` stages.
    # Names listed here are hidden from the tool list the LLM sees for just
    # this stage. Lets tasks like gather_context / research_library scope
    # out the plan_* authoring tools that only make sense in author_agents /
    # author_tasks. Ignored on framework / decision stages.
    hide_tools: tuple[str, ...] = ()


@dataclass
class StagedTask:
    """A :class:`Task` augmented with an ordered sequence of stages.

    The wrapped :class:`Task` provides the id, spec, postconditions, and agent
    assignment. The stages drive the actual LLM calls.
    """

    task: Task
    stages: list[Stage] = field(default_factory=list)


class StageRunner:
    """Execute a :class:`StagedTask` stage-by-stage, then evaluate the task spec.

    Built for hard-to-generate tasks where a single 20-iteration loop blows
    past num_ctx or lets the model drift. The runner reuses the underlying
    :class:`~agora.fleet.agent_runtime.AgentRuntime` machinery (system
    prompts, tool execution, postcondition evaluation) but resets the
    message history at every stage boundary, capping per-stage iterations
    aggressively. Two wins for weak models: fresh context per stage so
    accumulated tokens don't pile up, and one-action stages so the model
    decides less and hallucinates less.

    Three stage kinds are dispatched here:

    - ``"llm"`` (default) — a focused agent turn against the configured LLM.
    - ``"decision"`` — bypasses the LLM, posts a Matrix poll + reaction
      card, waits for the user's answer, writes the chosen option id to
      ``stage.output_path``.
    - ``"framework"`` — pure mechanical step (e.g. ``framework_finalize_plan``)
      that the framework executes itself rather than asking the LLM.
    """

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime
        self._llm = runtime._llm
        self._ctx = runtime._ctx

    async def execute_staged_task(
        self,
        staged: StagedTask,
        identity: AgentIdentity,
    ) -> TaskResult:
        """Run every stage in order, then evaluate the wrapped task's postconditions.

        Returns the same :class:`~agora.fleet.agent_runtime.TaskResult` shape
        as a non-staged task — postconditions are checked once at the end
        over the cumulative artefacts, so callers can treat staged and
        non-staged tasks uniformly.

        On stage failure (validation predicate returns False, decision-stage
        timeout, framework-stage exception), the runner short-circuits and
        returns a failure result without running later stages. The remaining
        stages do not execute.
        """
        task = staged.task
        self._ctx.expected_output_path = task.output_path
        self._ctx.task_focus = task.description or task.spec.description or task.id
        # v2.7: reset fill_test_body binding at the task boundary — a prior
        # task's scaffold shouldn't leak into this one's tool manifest.
        self._ctx.active_test_file = ""
        system_prompt = self._runtime._compose_system_prompt(identity, task_id=task.id)
        executor = get_tool_executor(identity.config.role, self._ctx)

        total_usage = {"input_tokens": 0, "output_tokens": 0}
        total_iterations = 0
        last_final_text = ""
        last_stop = ""

        for idx, stage in enumerate(staged.stages, start=1):
            label = stage.name or f"stage{idx}"
            logger.info(
                "stage %s/%d task=%s name=%s kind=%s",
                idx, len(staged.stages), task.id, label,
                getattr(stage, "kind", "llm"),
            )

            # v2.1: dispatch on stage.kind. "decision" stages bypass the LLM
            # loop entirely — the framework posts the question + options and
            # awaits the user's answer via poll / reaction / chat command,
            # then writes the chosen answer id to stage.output_path. This
            # guarantees the model can't paraphrase decision metadata.
            stage_kind = getattr(stage, "kind", "llm")
            if stage_kind == "decision":
                ok, reason = await self._execute_decision_stage(stage, task)
                if not ok:
                    return self._failure_result(
                        task,
                        stage_label=label,
                        reason=reason,
                        final_text=last_final_text,
                        iterations=total_iterations,
                        stop_reason=last_stop,
                        usage=total_usage,
                    )
                continue

            # v2.2: framework_finalize_plan — serialises ``ctx.plan_draft`` to
            # YAML and round-trips through ``load_plan``. No LLM involved;
            # the 7B planner proved it can't reliably call a specific tool
            # in the presence of similarly-named siblings, so the framework
            # just does the mechanical step itself when the prior LLM stages
            # have populated the draft.
            if stage_kind == "framework_finalize_plan":
                ok, reason = await self._execute_finalize_plan_stage(stage, task)
                if not ok:
                    return self._failure_result(
                        task,
                        stage_label=label,
                        reason=reason,
                        final_text=last_final_text,
                        iterations=total_iterations,
                        stop_reason=last_stop,
                        usage=total_usage,
                    )
                continue

            # v2.3: plan_reset_tasks — idempotent framework stage that clears
            # ``control.plan_draft.tasks`` so in-phase auto-retries of a
            # compound author task start from a clean draft instead of
            # appending to accumulated slot-N entries from prior attempts.
            if stage_kind == "plan_reset_tasks":
                self._execute_reset_tasks_stage()
                continue

            # v2.3 — agent-builder fleet framework stages. Each either mutates
            # the shared ``plan_draft`` or reads it, returning (ok, reason)
            # that is surfaced as a stage failure (failing the task, which
            # triggers in-phase auto-retry from the top of the compound task).
            if stage_kind == "plan_reset_agents":
                self._execute_reset_agents_stage()
                continue

            if stage_kind == "plan_snapshot_draft":
                ok, reason = self._execute_snapshot_draft_stage(stage)
                if not ok:
                    return self._failure_result(
                        task,
                        stage_label=label,
                        reason=reason,
                        final_text=last_final_text,
                        iterations=total_iterations,
                        stop_reason=last_stop,
                        usage=total_usage,
                    )
                continue

            if stage_kind == "plan_validate_agent":
                ok, reason = self._execute_validate_agent_stage(stage)
                if not ok:
                    return self._failure_result(
                        task,
                        stage_label=label,
                        reason=reason,
                        final_text=last_final_text,
                        iterations=total_iterations,
                        stop_reason=last_stop,
                        usage=total_usage,
                    )
                continue

            if stage_kind == "plan_validate_roster":
                ok, reason = self._execute_validate_roster_stage()
                if not ok:
                    return self._failure_result(
                        task,
                        stage_label=label,
                        reason=reason,
                        final_text=last_final_text,
                        iterations=total_iterations,
                        stop_reason=last_stop,
                        usage=total_usage,
                    )
                continue

            if stage_kind == "plan_validate_agents_vs_tasks":
                ok, reason = self._execute_validate_agents_vs_tasks_stage()
                if not ok:
                    return self._failure_result(
                        task,
                        stage_label=label,
                        reason=reason,
                        final_text=last_final_text,
                        iterations=total_iterations,
                        stop_reason=last_stop,
                        usage=total_usage,
                    )
                continue

            if stage_kind == "plan_link_tasks_to_agents":
                self._execute_link_tasks_to_agents_stage()
                continue

            # v2.4: test-authoring fleet — scaffold the test file skeleton
            # with correct imports + skipped-test stubs so the LLM stage only
            # has to fill in assertions (not reconstruct imports from a
            # guessed package name).
            if stage_kind == "plan_scaffold_tests":
                ok, reason = self._execute_scaffold_tests_stage(stage)
                if not ok:
                    return self._failure_result(
                        task,
                        stage_label=label,
                        reason=reason,
                        final_text=last_final_text,
                        iterations=total_iterations,
                        stop_reason=last_stop,
                        usage=total_usage,
                    )
                continue

            if stage_kind == "plan_run_pytest":
                # Observational — captures pytest stdout to a file for
                # debugging but never fails the stage (the pytest_passes
                # postcondition does the actual gating at task finalize).
                self._execute_run_pytest_stage(stage)
                continue

            # v2.6: plan_derive_test_intent — before the LLM fills assertions,
            # make one narrow LLM call per test function to derive what each
            # test should verify (prose, not code). Separates reading
            # comprehension from code emission so the 7B can handle each slice.
            if stage_kind == "plan_derive_test_intent":
                ok, reason = await self._execute_derive_test_intent_stage(
                    stage, identity
                )
                if not ok:
                    return self._failure_result(
                        task,
                        stage_label=label,
                        reason=reason,
                        final_text=last_final_text,
                        iterations=total_iterations,
                        stop_reason=last_stop,
                        usage=total_usage,
                    )
                continue

            user_msg = self._build_stage_user_message(stage, task)
            messages: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]

            # v2.7: compute manifest per-stage so the fill_test_body gate
            # (bound by the prior plan_scaffold_tests stage) takes effect
            # without leaking across stages/tasks.
            stage_tools = get_tool_definitions(
                identity.config.role,
                auto_hooks_enabled=self._ctx.auto_hooks_enabled,
                plan_authoring_enabled=self._ctx.plan_authoring_enabled,
                fill_test_body_bound=bool(self._ctx.active_test_file),
            )
            # v2.3: apply per-stage hide_tools filter. Stages like gather_context's
            # write_brief drifted into plan_upsert_agent / plan_add_task_spec calls
            # when those tools were globally visible; narrowing the manifest keeps
            # the LLM focused on the single action the stage is meant to perform.
            # (Note: write_file auto-hiding for existing output files is handled
            # per-turn inside _run_loop — don't duplicate it here.)
            if getattr(stage, "hide_tools", ()):
                _hidden = set(stage.hide_tools)
                stage_tools = [t for t in stage_tools if t["name"] not in _hidden]

            final_text, iters, stop, usage = await self._runtime._run_loop(
                messages=messages,
                system_prompt=system_prompt,
                tools=stage_tools,
                executor=executor,
                task_id=f"{task.id}:{label}",
                agent_name=identity.config.name,
                model=identity.config.model,
                max_iterations=stage.max_iterations,
            )
            _merge_usage(total_usage, usage)
            total_iterations += iters
            last_final_text = final_text or last_final_text
            last_stop = stop or last_stop

            if stage.validation is not None:
                ok, reason = stage.validation(self._ctx)
                if not ok:
                    logger.info(
                        "stage %s validation failed: %s", label, reason
                    )
                    return self._failure_result(
                        task,
                        stage_label=label,
                        reason=reason,
                        final_text=last_final_text,
                        iterations=total_iterations,
                        stop_reason=last_stop,
                        usage=total_usage,
                    )

        if self._ctx.auto_hooks_enabled:
            from agora.fleet.auto_hooks import synthesize_mark_complete

            synthesize_mark_complete(self._ctx, last_final_text)

        artifacts = _collect_artifacts(self._ctx)
        postcondition_results = _evaluate_postconditions(
            task, last_final_text, artifacts, self._ctx
        )
        success = all(passed for _, passed, _ in postcondition_results)

        reinforced_ids: list[str] = []
        if success and identity.learned_patterns:
            from agora.core.learning import filter_active

            reinforced_ids = [l.id for l in filter_active(list(identity.learned_patterns))]

        return TaskResult(
            task_id=task.id,
            success=success,
            output=last_final_text,
            artifacts=artifacts,
            postcondition_results=postcondition_results,
            reinforced_ids=reinforced_ids,
            token_usage=total_usage,
            iterations=total_iterations,
            stop_reason=last_stop,
        )

    async def _execute_decision_stage(
        self, stage, task: Task
    ) -> tuple[bool, str]:
        """Post a question to the project room + await the user's answer.

        Three voting surfaces (all feed ``control.resolve_decision``):
          1. MSC3381 poll widget (if Element renders it).
          2. Emoji reaction on the question-card message (1️⃣ … 9️⃣).
          3. ``/agora decision <answer_id>`` chat fallback (handled in control).

        Writes the resolved answer to ``stage.output_path`` under work_dir so
        downstream LLM stages read it via ``context_files``.
        """
        import asyncio as _asyncio
        from pathlib import Path as _Path

        from agora.observe.polls import POLL_START_TYPE, build_decision_poll

        control = getattr(self._ctx, "control", None)
        if control is None:
            return (False, "decision stage requires an observer-enabled project")

        decision_id = stage.decision_id
        options_tuple = tuple(stage.options)
        if len(options_tuple) < 2:
            return (False, f"decision stage {stage.name!r} needs ≥ 2 options")
        if len(options_tuple) > len(_EMOJI_DIGITS):
            return (
                False,
                f"decision stage {stage.name!r} has {len(options_tuple)} options; "
                f"max {len(_EMOJI_DIGITS)} supported",
            )

        # Build emoji→answer map for reaction voting.
        emoji_to_answer: dict[str, str] = {
            _EMOJI_DIGITS[i]: answer_id
            for i, answer_id in enumerate(options_tuple)
        }

        # Pre-register the future BEFORE posting anything so a very fast
        # response can't race the registration.
        loop = _asyncio.get_event_loop()
        future = loop.create_future()
        control.pending_decisions[decision_id] = future

        # 1) Post the plain question-card. Every Matrix client renders this.
        option_lines = "\n".join(
            f"  {_EMOJI_DIGITS[i]}  {aid}"
            for i, aid in enumerate(options_tuple)
        )
        card_body = (
            f"**Decision: {decision_id}**\n\n"
            f"{stage.question}\n\n"
            f"{option_lines}\n\n"
            f"React with the emoji above, click the poll, OR type "
            f"`/agora decision <answer_id>` in this room."
        )
        try:
            msg_event_id = await self._ctx.matrix_client.send_event(
                self._ctx.project_room_id,
                "m.room.message",
                {"msgtype": "m.text", "body": card_body},
            )
        except Exception as exc:  # noqa: BLE001
            control.pending_decisions.pop(decision_id, None)
            return (False, f"failed to post decision card: {exc}")
        control.register_decision_reactions(decision_id, msg_event_id, emoji_to_answer)

        # 2) Post the MSC3381 poll (best-effort). Renders as an interactive
        # widget in clients that support stable-schema polls.
        try:
            poll_content = build_decision_poll(
                stage.question,
                [(aid, aid) for aid in options_tuple],
                decision_id,
            )
            poll_event_id = await self._ctx.matrix_client.send_event(
                self._ctx.project_room_id, POLL_START_TYPE, poll_content
            )
            control.register_decision_poll(decision_id, poll_event_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "decision poll post failed (continuing — reactions + chat fallback still work): %s",
                exc,
            )

        # 3) Await the user's answer via any of the three surfaces.
        timeout_env = os.getenv("AGORA_DECISION_TIMEOUT_SECONDS")
        timeout = (
            float(timeout_env) if timeout_env else 300.0
        )
        try:
            answer_id = await control.await_decision(decision_id, timeout_seconds=timeout)
        except _asyncio.TimeoutError:
            return (False, f"decision {decision_id!r} timed out after {timeout}s")

        # 4) Write the answer to the stage's output_path.
        work_dir = _Path(self._ctx.work_dir)
        out_path = work_dir / stage.output_path
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(answer_id, encoding="utf-8")
        except OSError as exc:
            return (False, f"failed to write decision answer to {stage.output_path}: {exc}")

        # Record the answer as an artifact + completion so task-level
        # postconditions can evaluate against it and the write-event card posts.
        self._ctx.completions.append(
            {
                "summary": f"decision {decision_id} resolved: {answer_id}",
                "artifacts": [stage.output_path],
            }
        )
        if stage.output_path not in self._ctx.written_files:
            self._ctx.written_files.append(stage.output_path)

        logger.info(
            "decision stage resolved: decision=%s answer=%s output=%s",
            decision_id, answer_id, stage.output_path,
        )
        return (True, "")

    def _execute_reset_tasks_stage(self) -> None:
        """Clear ``plan_draft.tasks`` so subsequent author stages start clean.

        Keeps agents + metadata intact — only the task graph is reset. Used as
        the first stage of a compound author task so that in-phase retries
        don't accumulate duplicate task entries from prior attempts.
        """
        control = getattr(self._ctx, "control", None)
        draft = (
            getattr(control, "plan_draft", None)
            if control is not None
            else None
        ) or getattr(self._ctx, "plan_draft", None)
        if draft is None or not hasattr(draft, "tasks"):
            logger.info("plan_reset_tasks: no plan_draft to reset (skipping)")
            return
        prior = len(draft.tasks)
        draft.tasks.clear()
        logger.info("plan_reset_tasks: cleared %d prior task(s) from draft", prior)

    # v2.3 — Agent-builder fleet framework-stage helpers. All share the same
    # shape: resolve the draft from control → ctx, short-circuit with an
    # informative log when no draft exists, delegate to a PlanDraft validator
    # method, surface any problems list as a joined stage-failure reason.

    def _get_draft(self):
        """Return the shared PlanDraft or None. Prefers ``control.plan_draft``
        so agent/task mutations across tasks in one run see the same instance."""
        control = getattr(self._ctx, "control", None)
        draft = (
            getattr(control, "plan_draft", None)
            if control is not None
            else None
        ) or getattr(self._ctx, "plan_draft", None)
        return draft

    def _execute_reset_agents_stage(self) -> None:
        """Clear ``plan_draft.agents``. Mirrors ``plan_reset_tasks``; used as
        the first stage of ``author_agents`` so retry runs start fresh."""
        draft = self._get_draft()
        if draft is None or not hasattr(draft, "agents"):
            logger.info("plan_reset_agents: no plan_draft to reset (skipping)")
            return
        prior = draft.reset_agents() if hasattr(draft, "reset_agents") else len(
            draft.agents
        )
        logger.info("plan_reset_agents: cleared %d prior agent(s) from draft", prior)

    def _execute_snapshot_draft_stage(self, stage: Stage) -> tuple[bool, str]:
        """Write a markdown snapshot of the current draft to ``stage.output_path``.

        The subsequent LLM stage reads this file via ``context_files`` so it
        can see what agents + task_ids already exist when picking values for
        its own tool call (e.g. ``depends_on``).
        """
        draft = self._get_draft()
        if draft is None or not hasattr(draft, "snapshot_markdown"):
            return (
                False,
                "plan_snapshot_draft: plan_draft is empty — a prior author "
                "stage must run first",
            )
        out_rel = stage.output_path
        if not out_rel:
            return (False, "plan_snapshot_draft: output_path is required")
        out_abs = Path(self._ctx.work_dir) / out_rel
        try:
            out_abs.parent.mkdir(parents=True, exist_ok=True)
            out_abs.write_text(draft.snapshot_markdown(), encoding="utf-8")
        except OSError as exc:
            return (False, f"plan_snapshot_draft: write failed: {exc}")
        if out_rel not in self._ctx.written_files:
            self._ctx.written_files.append(out_rel)
        logger.info(
            "plan_snapshot_draft: wrote %s (%d agents, %d tasks)",
            out_rel, len(draft.agents), len(draft.tasks),
        )
        return (True, "")

    def _execute_validate_agent_stage(self, stage: Stage) -> tuple[bool, str]:
        """Compile-check one agent identified by ``validation_args.expected_name``
        against the current draft. Failure flows into task-level retry."""
        draft = self._get_draft()
        if draft is None or not hasattr(draft, "validate_agent"):
            return (False, "plan_validate_agent: no plan_draft on context")
        args = stage.validation_args or {}
        name = args.get("expected_name", "")
        expected_role = args.get("expected_role", "")
        if not name:
            return (
                False,
                "plan_validate_agent: validation_args.expected_name is required",
            )
        problems = draft.validate_agent(name, expected_role=expected_role)
        if problems:
            return (False, "plan_validate_agent: " + "; ".join(problems))
        logger.info("plan_validate_agent: %s OK (role=%s)", name, expected_role or "?")
        return (True, "")

    def _execute_validate_roster_stage(self) -> tuple[bool, str]:
        """Roster-level check: min count, builder-role presence, no dup pairs."""
        draft = self._get_draft()
        if draft is None or not hasattr(draft, "validate_roster"):
            return (False, "plan_validate_roster: no plan_draft on context")
        problems = draft.validate_roster()
        if problems:
            return (False, "plan_validate_roster: " + "; ".join(problems))
        logger.info("plan_validate_roster: OK (%d agents)", len(draft.agents))
        return (True, "")

    def _execute_link_tasks_to_agents_stage(self) -> None:
        """Re-balance task assignments so every agent has ≥1 task. Fills in
        missing ``assigned_to`` fields via role heuristic, then reassigns one
        task from over-loaded agents to any orphan agents. Always succeeds
        (no-op if there's nothing to fix)."""
        draft = self._get_draft()
        if draft is None or not hasattr(draft, "link_tasks_to_agents"):
            logger.info("plan_link_tasks_to_agents: no plan_draft to link (skipping)")
            return
        actions = draft.link_tasks_to_agents()
        filled = actions.get("filled") or []
        rebalanced = actions.get("rebalanced") or []
        if filled or rebalanced:
            logger.info(
                "plan_link_tasks_to_agents: filled=%s rebalanced=%s",
                filled, rebalanced,
            )
        else:
            logger.info("plan_link_tasks_to_agents: no action (all tasks assigned)")

    def _execute_scaffold_tests_stage(self, stage: Stage) -> tuple[bool, str]:
        """Write a scaffolded test file to ``stage.output_path``.

        Derives the scaffold from ``plan/brief.md`` (``## Key deliverables``
        bullets) and from a filesystem walk of ``src/`` (real module imports).
        Bypasses the write_file tool's overwrite guard because this is
        framework-owned authoring, not LLM-initiated — a retry's prior scaffold
        is idempotently replaced. After writing, records the path in
        ``ctx.written_files`` so the subsequent LLM stage sees write_file
        auto-hidden (per :func:`_output_path_has_content`).
        """
        from agora.plan.api_spec import parse_api_spec, render_test_imports
        from agora.plan.test_scaffolder import (
            discover_modules,
            parse_deliverables,
            render_scaffold,
        )

        out_rel = stage.output_path
        if not out_rel:
            return (False, "plan_scaffold_tests: output_path is required")
        work_dir = Path(self._ctx.work_dir)
        brief_path = work_dir / "plan" / "brief.md"
        try:
            brief_text = (
                brief_path.read_text(encoding="utf-8") if brief_path.is_file() else ""
            )
        except OSError as exc:
            return (False, f"plan_scaffold_tests: brief read failed: {exc}")
        deliverables = parse_deliverables(brief_text)
        # Fallback: loader passes a synthesized brief via validation_args
        # when the real brief.md isn't present in the executor workspace.
        if not deliverables:
            fallback = (stage.validation_args or {}).get("fallback_brief", "")
            if fallback:
                deliverables = parse_deliverables(fallback)
        # v2.5 contract-vs-impl mode. Contract mode omits module-level
        # imports (implementation doesn't exist yet). Default stays "impl"
        # so Sprint 5 behavior is preserved for any stages that don't
        # declare a mode in validation_args.
        mode = (stage.validation_args or {}).get("mode", "impl")
        modules = discover_modules(work_dir)
        # Sprint 7.5: when plan/api_spec.md is present (seed_workspace wrote
        # it + matching src/ stubs), generate REAL module-level imports from
        # the spec — the single source of truth tester and implementer both
        # reference. Tester can't hallucinate names because imports come
        # from the framework, not the LLM.
        api_spec_imports: list[str] | None = None
        spec_path = work_dir / "plan" / "api_spec.md"
        if spec_path.is_file():
            try:
                spec_modules = parse_api_spec(spec_path.read_text(encoding="utf-8"))
            except OSError:
                spec_modules = []
            if spec_modules:
                api_spec_imports = render_test_imports(spec_modules)
        if not deliverables and not modules and not api_spec_imports:
            logger.warning(
                "plan_scaffold_tests: brief has no deliverables AND src/ has no "
                "discoverable modules AND no api_spec — emitting a generic stub",
            )
        content = render_scaffold(
            modules,
            deliverables,
            mode=mode,
            api_spec_imports=api_spec_imports,
        )
        out_abs = work_dir / out_rel
        try:
            out_abs.parent.mkdir(parents=True, exist_ok=True)
            out_abs.write_text(content, encoding="utf-8")
        except OSError as exc:
            return (False, f"plan_scaffold_tests: write failed: {exc}")
        if out_rel not in self._ctx.written_files:
            self._ctx.written_files.append(out_rel)
        # v2.7: bind this as the active test file so the following
        # fill_assertions stage gets the fill_test_body tool in its manifest.
        # Cleared at the start of the next task by execute_staged_task.
        self._ctx.active_test_file = out_rel
        logger.info(
            "plan_scaffold_tests: wrote %s (mode=%s, %d deliverables, %d modules)",
            out_rel, mode, len(deliverables), len(modules),
        )
        return (True, "")

    async def _execute_derive_test_intent_stage(
        self, stage: Stage, identity: AgentIdentity
    ) -> tuple[bool, str]:
        """Per-test LLM call that writes plain-prose intent to a markdown file.

        Parses the scaffolded test file for ``test_*`` function names, then
        issues ONE narrow LLM call per function (no tools) asking the model
        to describe in 2-4 sentences what the test should verify, given the
        brief. Writes the result as ``## test_<name>`` sections to
        ``stage.output_path``. The next (fill_assertions) stage reads this
        intent file as context so the LLM has a pre-extracted anchor of what
        each assertion is testing — no inference during code emission.

        Brief source: ``work_dir/plan/brief.md`` if present, else the
        ``fallback_brief`` validation_arg (plan v2.6 embeds the rich brief
        there). ``scaffold_path`` validation_arg says where the test file
        is. ``mode`` distinguishes contract (no src access) vs impl
        (reads src/ for real API shapes).
        """
        import ast

        out_rel = stage.output_path or "plan/kb/test_intent.md"
        work_dir = Path(self._ctx.work_dir)
        args = stage.validation_args or {}
        scaffold_rel = args.get("scaffold_path", "")
        if not scaffold_rel:
            return (False, "plan_derive_test_intent: scaffold_path validation_arg required")
        scaffold_abs = work_dir / scaffold_rel
        if not scaffold_abs.is_file():
            return (
                False,
                f"plan_derive_test_intent: scaffold file {scaffold_rel!r} not found",
            )
        try:
            source = scaffold_abs.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError) as exc:
            return (
                False,
                f"plan_derive_test_intent: couldn't parse {scaffold_rel}: {exc}",
            )
        tests: list[tuple[str, str]] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    tests.append((node.name, ast.get_docstring(node) or ""))
        if not tests:
            return (
                False,
                f"plan_derive_test_intent: no test_* functions in {scaffold_rel}",
            )

        # Brief — prefer the real file, fall back to embedded validation_args.
        brief_text = ""
        brief_path = work_dir / "plan" / "brief.md"
        if brief_path.is_file():
            try:
                brief_text = brief_path.read_text(encoding="utf-8")
            except OSError:
                pass
        if not brief_text:
            brief_text = args.get("fallback_brief", "")
        if not brief_text:
            return (
                False,
                "plan_derive_test_intent: no brief available "
                "(neither plan/brief.md nor fallback_brief in validation_args)",
            )

        mode = args.get("mode", "contract")
        mode_note = (
            "The implementation does NOT exist yet; describe behavioral "
            "invariants only (type/length/round-trip). Do not guess "
            "specific output values."
            if mode == "contract"
            else "The implementation exists; describe concrete input→output "
            "observations and edge cases a test should cover."
        )

        sections: list[str] = [
            "# Test intent",
            "",
            f"One section per test function in {scaffold_rel}. "
            "Feed this to the fill_assertions stage as a context file.",
            "",
        ]
        for test_name, doc in tests:
            prompt = (
                "Describe what ONE pytest test function should verify, in "
                "2-4 sentences of prose. No code. No tool calls.\n\n"
                "Project brief:\n"
                f"{brief_text.strip()}\n\n"
                f"Test function: `{test_name}`\n"
                f"Hint (from scaffold docstring): {doc!r}\n\n"
                f"{mode_note}\n\n"
                "Output format: one short paragraph. Name the project "
                "feature this test covers, state the success criterion, "
                "and note what a failing assertion would indicate about "
                "the implementation."
            )
            try:
                resp = await self._runtime._llm.complete(
                    messages=[{"role": "user", "content": prompt}],
                    system="",
                    tools=None,
                    model=identity.config.model,
                    max_tokens=512,
                )
            except Exception as exc:  # noqa: BLE001
                return (
                    False,
                    f"plan_derive_test_intent: LLM call failed for "
                    f"{test_name}: {exc}",
                )
            intent_prose = (resp.content or "").strip()
            if not intent_prose:
                intent_prose = "(LLM returned no content; intent missing)"
            sections.append(f"## {test_name}")
            sections.append("")
            sections.append(intent_prose)
            sections.append("")

        out_abs = work_dir / out_rel
        try:
            out_abs.parent.mkdir(parents=True, exist_ok=True)
            out_abs.write_text("\n".join(sections), encoding="utf-8")
        except OSError as exc:
            return (False, f"plan_derive_test_intent: write failed: {exc}")
        if out_rel not in self._ctx.written_files:
            self._ctx.written_files.append(out_rel)
        logger.info(
            "plan_derive_test_intent: wrote %s (%d tests, mode=%s)",
            out_rel, len(tests), mode,
        )
        return (True, "")

    def _execute_run_pytest_stage(self, stage: Stage) -> None:
        """Run pytest against the project and save stdout to ``stage.output_path``.

        Never fails the stage — this is observational only so the failure
        output is persisted for post-mortem / retry context. The task's
        ``pytest_passes`` postcondition does the actual gating.
        """
        from agora.fleet._subprocess import format_failure, run_host_python

        out_rel = stage.output_path or "plan/kb/pytest_output.md"
        work_dir = Path(self._ctx.work_dir)
        try:
            result = run_host_python(
                ["-m", "pytest", "tests/", "-q", "--tb=short", "--no-header",
                 "-p", "no:cacheprovider"],
                cwd=str(work_dir),
                timeout=60.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("plan_run_pytest: pytest invocation failed: %s", exc)
            return
        lines = [
            "# pytest output",
            "",
            f"**exit code**: {getattr(result, 'returncode', '?')}",
            f"**passed**: {getattr(result, 'ok', False)}",
            "",
            "## stdout + stderr",
            "```",
            format_failure(result, limit=8000),
            "```",
        ]
        out_abs = work_dir / out_rel
        try:
            out_abs.parent.mkdir(parents=True, exist_ok=True)
            out_abs.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            logger.warning("plan_run_pytest: write to %s failed: %s", out_rel, exc)
            return
        if out_rel not in self._ctx.written_files:
            self._ctx.written_files.append(out_rel)
        logger.info(
            "plan_run_pytest: %s (exit=%s, wrote %s)",
            "pass" if getattr(result, "ok", False) else "FAIL",
            getattr(result, "returncode", "?"),
            out_rel,
        )

    def _execute_validate_agents_vs_tasks_stage(self) -> tuple[bool, str]:
        """Coverage cross-check: every agent has ≥1 task, every task's
        assigned_to exists in the roster."""
        draft = self._get_draft()
        if draft is None or not hasattr(draft, "validate_agents_vs_tasks"):
            return (
                False,
                "plan_validate_agents_vs_tasks: no plan_draft on context",
            )
        problems = draft.validate_agents_vs_tasks()
        if problems:
            return (
                False,
                "plan_validate_agents_vs_tasks: " + "; ".join(problems),
            )
        logger.info(
            "plan_validate_agents_vs_tasks: OK (%d agents × %d tasks)",
            len(draft.agents), len(draft.tasks),
        )
        return (True, "")

    async def _execute_finalize_plan_stage(
        self, stage, task: Task
    ) -> tuple[bool, str]:
        """Serialise ``ctx.plan_draft`` to YAML + round-trip through ``load_plan``.

        The prior ``author_*`` LLM stages populate ``ctx.plan_draft`` via the
        typed plan_* tools. This stage is mechanical — no LLM, no tool calls —
        so the 7B planner can't paraphrase the finalization step into another
        ``plan_add_task`` call (which we observed repeatedly in live runs).
        """
        from pathlib import Path as _Path

        from agora.core.flow import save_flow
        from agora.plan.loader import instantiate_plan, load_plan

        # Prefer the shared draft on control (persists across task boundaries);
        # fall back to ctx.plan_draft for standalone / test contexts.
        control = getattr(self._ctx, "control", None)
        draft = (
            getattr(control, "plan_draft", None)
            if control is not None
            else None
        ) or getattr(self._ctx, "plan_draft", None)
        if draft is None or not hasattr(draft, "tasks"):
            return (
                False,
                "framework_finalize_plan: plan_draft is empty — author_* "
                "stages must run first",
            )
        # v2.6 — embed the rich brief into the emitted plan.yaml so the
        # executor's scaffolder + LLM stages have real deliverable context
        # (the executor starts in a fresh work_dir with no plan/brief.md).
        # Only populate if the draft doesn't already have one; author
        # stages may have set it explicitly via tools in the future.
        if not getattr(draft, "brief", ""):
            brief_path = _Path(self._ctx.work_dir) / "plan" / "brief.md"
            if brief_path.is_file():
                try:
                    draft.brief = brief_path.read_text(encoding="utf-8")
                except OSError:
                    pass  # non-fatal — brief stays empty
        # v2.7 — same treatment for plan/api_spec.md. When the plan-builder
        # has a define_api task that wrote this file, we embed its content
        # so executor scaffolders can generate matching test imports + src
        # stubs from the single shared API truth.
        if not getattr(draft, "api_spec", ""):
            spec_path = _Path(self._ctx.work_dir) / "plan" / "api_spec.md"
            if spec_path.is_file():
                try:
                    draft.api_spec = spec_path.read_text(encoding="utf-8")
                except OSError:
                    pass
        problems = draft.validate_ready()
        if problems:
            return (
                False,
                "framework_finalize_plan: draft not ready — "
                + "; ".join(problems),
            )

        out_rel = stage.output_path
        out_abs = _Path(self._ctx.work_dir) / out_rel
        out_abs.parent.mkdir(parents=True, exist_ok=True)

        try:
            flow = draft.to_flow()
        except Exception as exc:  # noqa: BLE001
            return (False, f"framework_finalize_plan: to_flow failed: {exc}")
        try:
            save_flow(flow, out_abs)
        except Exception as exc:  # noqa: BLE001
            return (False, f"framework_finalize_plan: save_flow failed: {exc}")

        # Round-trip validation: proves the emitted YAML loads + instantiates.
        try:
            reloaded = load_plan(out_abs)
            project_name = draft.name or "_validate"
            r_agents, r_tasks, r_staged = instantiate_plan(
                reloaded, project_name=project_name
            )
        except Exception as exc:  # noqa: BLE001
            return (
                False,
                f"framework_finalize_plan: emitted YAML at {out_rel!r} "
                f"failed round-trip: {exc}",
            )

        # Record as an artifact + completion so task-level postconditions
        # (file_exists) evaluate green and the write-event card posts.
        if out_rel not in self._ctx.written_files:
            self._ctx.written_files.append(out_rel)
        self._ctx.completions.append(
            {
                "summary": (
                    f"plan finalized: {len(r_agents)} agents, {len(r_tasks)} tasks, "
                    f"{len(r_staged)} staged"
                ),
                "artifacts": [out_rel],
            }
        )
        logger.info(
            "framework_finalize_plan: wrote %s (%d agents, %d tasks, %d staged)",
            out_rel, len(r_agents), len(r_tasks), len(r_staged),
        )
        return (True, "")

    def _build_stage_user_message(self, stage: Stage, task: Task) -> str:
        parts: list[str] = []
        if task.output_path:
            parts.append(build_output_path_banner(task.output_path))
            parts.append("")
        parts.append(stage.instruction.strip())
        for rel in stage.context_files:
            body = self._read_context_file(rel)
            if body is None:
                parts.append(f"\n# File: {rel} (NOT FOUND)")
            else:
                parts.append(f"\n# File: {rel}\n```\n{body}\n```")
        return "\n".join(parts)

    def _read_context_file(self, rel: str) -> str | None:
        path = Path(self._ctx.work_dir) / rel
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _failure_result(
        self,
        task: Task,
        *,
        stage_label: str,
        reason: str,
        final_text: str,
        iterations: int,
        stop_reason: str,
        usage: dict[str, int],
    ) -> TaskResult:
        return TaskResult(
            task_id=task.id,
            success=False,
            output=final_text,
            artifacts=_collect_artifacts(self._ctx),
            postcondition_results=[
                (f"stage_{stage_label}", False, reason),
            ],
            token_usage=usage,
            iterations=iterations,
            stop_reason=stop_reason,
        )


__all__ = ["Stage", "StagedTask", "StageRunner"]
