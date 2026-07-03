"""Per-agent execution loop.

Given a :class:`Task` and an :class:`AgentIdentity`, the runtime:

1. Builds a system prompt from ``identity.effective_instructions``.
2. Constructs a user message from the task description plus postcondition hints.
3. Enters a tool-call loop: LLM → tool execution → results fed back, bounded
   by ``max_iterations``.
4. Validates postconditions against a result context and returns a
   :class:`TaskResult`.
5. Asks the LLM for a short post-task reflection to extract structured
   :class:`Learning` records, then appends them to the agent's identity room.

The runtime is deliberately storage-agnostic: all Matrix side effects go through
the injected :class:`MatrixClientProtocol`. All LLM calls go through
:class:`LLMProtocol`. Tests inject fakes for both.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agora.core.agent import AgentIdentity
from agora.core.contract import evaluate_postconditions
from agora.core.learning import Learning
from agora.core.task import Task
from agora.core.types import LearningCategory, TaskId
from agora.fleet.inner_tools import (
    ToolContext,
    get_tool_definitions,
    get_tool_executor,
)
from agora.fleet.llm_adapter import LLMProtocol, LLMResponse, ToolCall
from agora.matrix.client import MatrixClientProtocol
from agora.matrix.events import LEARNING_EVENT, learning_to_content

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    """Outcome of a single :meth:`AgentRuntime.execute_task` call.

    ``success`` is determined by the postconditions, not the LLM's
    self-report (see :class:`agora.core.contract.Specification`).
    ``postcondition_results`` lists ``(predicate_name, passed, reason)``
    triples for every predicate evaluated; ``artifacts`` are the file paths
    written through the inner tools; ``learnings`` are the failure traces
    extracted from this turn for the next agent's prompt.
    """

    task_id: TaskId
    success: bool
    output: str
    artifacts: list[str] = field(default_factory=list)
    postcondition_results: list[tuple[str, bool, str]] = field(default_factory=list)
    learnings: list[Learning] = field(default_factory=list)
    reinforced_ids: list[str] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)
    iterations: int = 0
    stop_reason: str = ""
    # --- observability (JSONL schema v1) — all optional, default-zero so
    # callers that don't read them are unaffected. Populated from the
    # per-attempt tool-call accumulator (see :class:`_ToolCallStats`). ---
    tool_calls_total: int = 0
    tool_calls_structured: int = 0
    tool_calls_text_fallback: int = 0
    turns_with_text_fallback: int = 0
    tool_calls_malformed: int = 0
    tool_call_unknown_name: int = 0
    tools_used: list[str] = field(default_factory=list)
    first_text_fallback_iteration: int | None = None
    duration_s: float = 0.0
    # v3 near-miss capture (findings S4). On a postcondition failure where the
    # output file WAS written (equality near-miss), the first 2KB of the actual
    # bytes, so a wrong-bytes failure is diagnosable from JSONL alone.
    artifact_capture: dict[str, Any] | None = None


@dataclass
class _ToolCallStats:
    """Per-task-attempt accumulator of model tool-call fidelity signals.

    Lives on one :class:`AgentRuntime` instance (created fresh per task
    attempt), so it aggregates across the stages of a staged task while
    staying scoped to a single attempt. Synthetic auto-hook calls are NOT
    fed here — only the model's own tool calls.

    The three primary counters share the **tool call** unit so they
    reconcile: ``structured + text_fallback == total``. The adapter's text
    fallback only runs when the native ``tool_calls`` field was empty, so a
    given turn's calls are wholly one origin — never a mix — which is what
    makes the invariant exact. ``malformed`` / ``unknown_name`` are overlap
    counters (a call counted there is also in one origin bucket).
    ``turns_with_text_fallback`` is a turn-level side channel, NOT a call count.
    """

    total: int = 0
    structured: int = 0
    text_fallback: int = 0
    turns_with_text_fallback: int = 0
    malformed: int = 0
    unknown_name: int = 0
    tools_used: set[str] = field(default_factory=set)
    first_text_fallback_iteration: int | None = None

    def note_turn(
        self,
        calls: list[ToolCall],
        results: list[str],
        *,
        from_text_fallback: bool,
        iteration: int,
    ) -> None:
        """Record one turn's model tool calls + their result strings.

        ``from_text_fallback`` flags that the adapter parsed this turn's calls
        out of prose ``content`` (the whole turn, since fallback only runs when
        the structured field was empty). ``iteration`` is the 0-based loop turn,
        used for ``first_text_fallback_iteration``.
        """
        n = len(calls)
        self.total += n
        if from_text_fallback and n:
            self.text_fallback += n
            self.turns_with_text_fallback += 1
            if self.first_text_fallback_iteration is None:
                self.first_text_fallback_iteration = iteration
        else:
            self.structured += n
        for call, result in zip(calls, results, strict=True):
            self.tools_used.add(call.name)
            res = result or ""
            if res.startswith("ERROR: unknown tool"):
                self.unknown_name += 1
            elif res.startswith("ERROR: tool "):
                self.malformed += 1

    def apply_to(self, result: "TaskResult") -> "TaskResult":
        """Return ``result`` with the accumulated tool-call fields populated."""
        from dataclasses import replace as _replace

        return _replace(
            result,
            tool_calls_total=self.total,
            tool_calls_structured=self.structured,
            tool_calls_text_fallback=self.text_fallback,
            turns_with_text_fallback=self.turns_with_text_fallback,
            tool_calls_malformed=self.malformed,
            tool_call_unknown_name=self.unknown_name,
            tools_used=sorted(self.tools_used),
            first_text_fallback_iteration=self.first_text_fallback_iteration,
        )


class AgentRuntime:
    """The per-task tool-calling loop that backs every Agora run.

    Each task goes through one ``execute_task`` call: build the system
    prompt from the agent's identity (including any active learnings),
    enter a bounded LLM ↔ tool loop, then evaluate postconditions against
    the produced artefacts. The runtime is storage-agnostic — Matrix side
    effects flow through the injected ``MatrixClientProtocol``, LLM calls
    through ``LLMProtocol``, and tools through the ``ToolContext`` — so it
    can run end-to-end against fakes in tests.

    Composition: the orchestrator holds one ``AgentRuntime`` per agent;
    staged tasks delegate to :class:`agora.fleet.stage_runner.StageRunner`,
    which reuses the same ``_run_loop`` internals with a fresh per-stage
    message history.
    """

    def __init__(
        self,
        llm: LLMProtocol,
        matrix_client: MatrixClientProtocol,
        tool_context: ToolContext,
    ) -> None:
        self._llm = llm
        self._matrix = matrix_client
        self._ctx = tool_context
        # Per-attempt tool-call fidelity accumulator (JSONL schema v1). The
        # runtime is constructed fresh per task attempt, so this scopes to one
        # attempt yet aggregates across a staged task's stages.
        self.tool_stats = _ToolCallStats()
        # Authoritative 0-based iteration index for the active tool loop turn,
        # set in ``_run_loop``. ``_fallback_this_turn`` is flipped by the
        # adapter hook when the current turn's calls came from text fallback;
        # ``_run_loop`` reads it to attribute the turn's call count correctly.
        self._cur_iter0: int = 0
        self._fallback_this_turn: bool = False
        # Wire the adapter's text-fallback hook when the adapter exposes one
        # (OllamaAdapter). Default-None adapters / fakes are left untouched.
        if hasattr(self._llm, "on_text_fallback"):
            self._llm.on_text_fallback = self._note_text_fallback

    def _note_text_fallback(self, _adapter_iteration: int) -> None:
        """Hook target for the LLM adapter's text-fallback signal.

        Pure flag-setter: the per-turn call-count attribution + first-iteration
        recording happen in :meth:`_ToolCallStats.note_turn` using the runtime's
        authoritative loop iteration (the adapter-supplied index can drift if
        the adapter is shared for side calls like distillation).
        """
        self._fallback_this_turn = True

    async def execute_task(
        self,
        task: Task,
        identity: AgentIdentity,
        max_iterations: int = 20,
    ) -> TaskResult:
        """Run one task end-to-end against the configured LLM and tools.

        Builds the system prompt from ``identity.effective_instructions``
        (which folds in any active learnings), enters the tool-call loop
        bounded by ``max_iterations``, evaluates postconditions against
        the result context, then asks the LLM for a short post-task
        reflection to extract structured :class:`~agora.core.learning.Learning`
        records.

        ``success`` on the returned :class:`TaskResult` is True iff every
        postcondition evaluates ``True`` — the LLM's ``mark_complete`` call
        is observed but does not determine outcome. On weak models that exit
        without ``mark_complete``, the auto-hooks layer synthesises one
        from the written files so the predicate has something to observe.
        """
        self._ctx.expected_output_path = task.output_path
        self._ctx.task_focus = task.description or task.spec.description or task.id
        system_prompt = self._compose_system_prompt(identity, task_id=task.id)
        tools = get_tool_definitions(
            identity.config.role,
            auto_hooks_enabled=self._ctx.auto_hooks_enabled,
            plan_authoring_enabled=self._ctx.plan_authoring_enabled,
        )
        # Note: write_file auto-hiding is applied per-turn inside _run_loop
        # (so within-stage cycling is caught too), not here.
        executor = get_tool_executor(identity.config.role, self._ctx)

        user_msg = self._build_user_message(task)
        # v2.7 Sprint 7.5(b): if the task's output file is a pre-written
        # stub (api_spec + seed_workspace dropped it there with NotImplementedError
        # bodies), tell the model explicitly to REPLACE the bodies rather
        # than let it guess. Observed failure: weak models see the file
        # exists, assume the task is done, return 0 tool calls for 3
        # retries, burn the budget.
        stub_hint = _build_stub_awareness_hint(task, self._ctx.work_dir)
        if stub_hint:
            user_msg = f"{user_msg}\n\n{stub_hint}"
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_msg}
        ]

        final_text, iterations, last_stop, total_usage = await self._run_loop(
            messages=messages,
            system_prompt=system_prompt,
            tools=tools,
            executor=executor,
            task_id=task.id,
            agent_name=identity.config.name,
            model=identity.config.model,
            max_iterations=max_iterations,
        )

        # When the LLM exits without calling mark_complete (dominant failure
        # mode on weak models), synthesize a completion from written files so
        # postcondition_mark_complete still has something to observe.
        if self._ctx.auto_hooks_enabled:
            from agora.fleet.auto_hooks import synthesize_mark_complete

            synthesize_mark_complete(self._ctx, final_text)

        artifacts = _collect_artifacts(self._ctx)
        postcondition_results = _evaluate_postconditions(task, final_text, artifacts, self._ctx)
        success = all(passed for _, passed, _ in postcondition_results)
        artifact_capture = _capture_failed_artifact(
            task.output_path or "", success, self._ctx.work_dir
        )

        learnings = await self._extract_learnings(task, final_text, identity, success)
        await self._persist_learnings(identity, learnings)

        # Flywheel: on success, reinforce every active pre-existing learning
        # that was injected into the system prompt this turn. Failure → no
        # reinforcement, natural decay applies next run.
        reinforced_ids: list[str] = []
        if success and identity.learned_patterns:
            from agora.core.learning import filter_active

            reinforced_ids = [l.id for l in filter_active(list(identity.learned_patterns))]

        return self.tool_stats.apply_to(
            TaskResult(
                task_id=task.id,
                success=success,
                output=final_text,
                artifacts=artifacts,
                postcondition_results=postcondition_results,
                learnings=learnings,
                reinforced_ids=reinforced_ids,
                token_usage=total_usage,
                iterations=iterations,
                stop_reason=last_stop,
                artifact_capture=artifact_capture,
            )
        )

    async def _run_loop(
        self,
        *,
        messages: list[dict[str, Any]],
        system_prompt: str,
        tools: list[dict[str, Any]],
        executor: dict[str, Any],
        task_id: str,
        agent_name: str,
        model: str,
        max_iterations: int,
    ) -> tuple[str, int, str, dict[str, int]]:
        """Run the tool-use loop. Returns ``(final_text, iterations, stop_reason, usage)``.

        Shared by :meth:`execute_task` and the :class:`~agora.fleet.stage_runner.StageRunner`,
        so auto-hook dispatch and synthetic message shaping live in one place.
        """
        from agora.fleet.auto_hooks import run_auto_hooks

        total_usage = {"input_tokens": 0, "output_tokens": 0}
        iterations = 0
        final_text = ""
        last_stop = ""
        nudges_used = 0
        # Per-tool JSON schemas for the v3 tool-boundary contract (S1). Stable
        # across turns; used to validate calls and to render CorrectiveErrors.
        schemas = {
            t["name"]: (t.get("input_schema") or t.get("parameters") or {})
            for t in tools
        }

        for iterations in range(1, max_iterations + 1):
            logger.info(
                "llm call: task=%s agent=%s turn=%d msgs=%d",
                task_id, agent_name, iterations, len(messages),
            )
            # v2.4: per-turn tool filter. If the task's output file already
            # has content (a prior turn wrote it, or a prior task retry left
            # it behind), drop write_file from this turn's manifest. The
            # 7B otherwise cycles through write_file → ERROR → retry within
            # the same stage, burning iterations.
            from agora.fleet.stage_runner import _output_path_has_content as _has

            turn_tools = (
                [t for t in tools if t["name"] != "write_file"]
                if _has(self._ctx)
                else tools
            )
            # Authoritative 0-based iteration index for the text-fallback hook;
            # reset the per-turn fallback flag before the call (the hook fires
            # inside complete() and flips it).
            self._cur_iter0 = iterations - 1
            self._fallback_this_turn = False
            resp: LLMResponse = await self._llm.complete(
                messages=messages,
                system=system_prompt,
                tools=turn_tools,
                model=model,
            )
            logger.info(
                "llm return: task=%s turn=%d tool_calls=%d content_len=%d",
                task_id, iterations, len(resp.tool_calls), len(resp.content or ""),
            )
            _merge_usage(total_usage, resp.usage)
            last_stop = resp.stop_reason
            final_text = resp.content or final_text

            if not resp.tool_calls:
                # v3 completion nudge (S2). A dead loop (0 tool calls) with the
                # expected output still unwritten and budget remaining gets ONE
                # corrective user turn and continues; otherwise terminate exactly
                # as v2. nudge_budget=0 (default) ⇒ this branch never fires ⇒
                # byte-identical to v2.
                missing = _expected_output_missing(self._ctx)
                if missing is not None and nudges_used < self._ctx.nudge_budget:
                    nudges_used += 1
                    logger.info(
                        "completion nudge %d/%d: task=%s expected output %r not written",
                        nudges_used, self._ctx.nudge_budget, task_id, missing,
                    )
                    messages.append(_assistant_turn(self._llm, resp))
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Not complete: expected output {missing} has not been "
                            "written. Continue with a tool call."
                        ),
                    })
                    continue
                break

            messages.append(_assistant_turn(self._llm, resp))
            call_list = list(resp.tool_calls)
            results = [
                await _dispatch_tool(
                    call, executor,
                    schema=schemas.get(call.name),
                    mode=self._ctx.tool_errors,
                )
                for call in call_list
            ]
            # Record model tool-call fidelity signals (synthetic auto-hook
            # calls below are intentionally excluded — they aren't model-emitted).
            self.tool_stats.note_turn(
                call_list,
                results,
                from_text_fallback=self._fallback_this_turn,
                iteration=self._cur_iter0,
            )
            # Diagnostic: log each tool call name + short result so live runs
            # are debuggable. Trimmed args mimic the minimum context reviewers
            # need to see what the model actually tried.
            for c, r in zip(call_list, results, strict=True):
                try:
                    arg_preview = str(c.arguments)[:120]
                except Exception:
                    arg_preview = "<unserializable>"
                res_preview = (r or "").replace("\n", " ")[:120]
                logger.info(
                    "tool call: task=%s turn=%d name=%s args=%s result=%s",
                    task_id, iterations, c.name, arg_preview, res_preview,
                )
            _append_turn(messages, _tool_results_turn(self._llm, call_list, results))

            # Auto-hooks: fire validation/git automatically after tool calls so
            # weak models see errors next turn without having to remember to
            # invoke the tools themselves.
            if self._ctx.auto_hooks_enabled:
                hook_calls: list[ToolCall] = []
                hook_results: list[str] = []
                for call, result in zip(call_list, results, strict=True):
                    hooks = await run_auto_hooks(
                        call.name, call.arguments, result, self._ctx
                    )
                    for idx, h in enumerate(hooks):
                        synthetic_id = f"{call.id}-auto-{idx}-{h.tool_name}"
                        hook_calls.append(
                            ToolCall(
                                id=synthetic_id,
                                name=h.tool_name,
                                arguments=h.arguments,
                            )
                        )
                        hook_results.append(h.result)
                    # Observer-side: post a compact card to the project room
                    # so the reviewer watches the build in real time.
                    if hooks:
                        await self._emit_write_event_card(
                            task_id=task_id,
                            call_name=call.name,
                            call_arguments=call.arguments,
                            call_result=result,
                            hooks=hooks,
                        )
                if hook_calls:
                    synthetic_resp = LLMResponse(content="", tool_calls=tuple(hook_calls))
                    messages.append(_assistant_turn(self._llm, synthetic_resp))
                    _append_turn(
                        messages,
                        _tool_results_turn(self._llm, hook_calls, hook_results),
                    )
        else:
            logger.warning(
                "agent_runtime: max_iterations (%d) reached for task %s", max_iterations, task_id
            )

        return final_text, iterations, last_stop, total_usage

    async def _emit_write_event_card(
        self,
        *,
        task_id: str,
        call_name: str,
        call_arguments: dict[str, Any],
        call_result: str,
        hooks: list[Any],
    ) -> None:
        """Post a human-readable write-event card to the project room."""
        try:
            from pathlib import Path

            from agora.observe.formatters import format_write_event

            rel = _path_from_call(call_name, call_arguments, call_result)
            if not rel:
                return
            operation = _operation_label(call_name, call_arguments, call_result)
            size = 0
            preview: str | None = None
            try:
                path = Path(self._ctx.work_dir) / rel
                if path.is_file():
                    size = path.stat().st_size
                    if size <= 4096:
                        preview = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
            summary_list = [(h.tool_name, h.success) for h in hooks]
            msg = format_write_event(
                task_id=task_id,
                path=rel,
                operation=operation,
                size_bytes=size,
                hook_summary=summary_list,
                preview=preview,
            )
            event_id = await self._matrix.send_event(
                self._ctx.project_room_id, "m.room.message", msg.to_content()
            )
            # Remember this card → task_id so reactions / replies route back.
            control = getattr(self._ctx, "control", None)
            if control is not None and hasattr(control, "register_task_card"):
                try:
                    control.register_task_card(event_id, task_id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("register_task_card failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            # Observer visibility must never fail the task.
            logger.warning("emit_write_event_card failed: %s", exc)

    # --------------------------------------------------------------------- helpers

    def _compose_system_prompt(
        self, identity: AgentIdentity, *, task_id: str = ""
    ) -> str:
        """Base effective instructions + observer notes + one-shot redirect + task comments."""
        base = identity.effective_instructions or ""
        control = getattr(self._ctx, "control", None)
        if control is None:
            return base
        sections: list[str] = [base] if base else []
        notes = control.consume_notes() if hasattr(control, "consume_notes") else []
        if notes:
            sections.append(
                "## Observer notes (latest)\n" + "\n".join(f"- {n}" for n in notes)
            )
        redirect = (
            control.consume_redirect(identity.config.name)
            if hasattr(control, "consume_redirect")
            else None
        )
        if redirect:
            sections.append(f"## Observer redirect\n{redirect}")
        if task_id and hasattr(control, "consume_task_comments"):
            task_comments = control.consume_task_comments(task_id) or []
            if task_comments:
                sections.append(
                    "## Reviewer feedback on this specific task\n"
                    + "\n".join(f"- {c}" for c in task_comments)
                    + "\n\nAddress this feedback in your next attempt."
                )
        return "\n\n".join(sections)

    @staticmethod
    def _build_user_message(task: Task) -> str:
        lines: list[str] = []
        if task.output_path:
            lines.append(build_output_path_banner(task.output_path))
            lines.append("")
        lines.append(f"Task: {task.description}")
        if task.spec.description:
            lines.append(f"Specification: {task.spec.description}")
        if task.spec.postconditions:
            lines.append("Postconditions (your output must satisfy these):")
            for p in task.spec.postconditions:
                lines.append(f"  - {p.name}: {p.description}")
        lines.append("")
        lines.append(
            "When finished, call `mark_complete` with a summary and list any artifact paths."
        )
        return "\n".join(lines)

    async def _extract_learnings(
        self,
        task: Task,
        result: str,
        identity: AgentIdentity,
        success: bool,
    ) -> list[Learning]:
        """Ask the LLM for a reflection; parse JSON array of learnings."""
        reflection_prompt = (
            "Reflect on the task just completed. Extract at most 3 reusable learnings as a "
            "JSON array of objects with fields: category "
            "(one of: pattern, failure, preference, tool_usage), content (one actionable sentence), "
            "confidence (0.0-1.0). Return ONLY the JSON array."
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"Task: {task.description}\n"
                    f"Outcome: {'success' if success else 'failure'}\n"
                    f"Final output: {result[:2000]}\n\n{reflection_prompt}"
                ),
            }
        ]
        try:
            resp = await self._llm.complete(
                messages=messages,
                system="You produce terse, actionable reflections as strict JSON.",
                model=identity.config.model,
                max_tokens=1024,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("learning extraction failed for task %s: %s", task.id, exc)
            return []

        return _parse_learnings(resp.content, task_ref=task.id)

    async def _persist_learnings(
        self, identity: AgentIdentity, learnings: list[Learning]
    ) -> None:
        for learning in learnings:
            await self._matrix.send_event(
                identity.room_id,
                LEARNING_EVENT,
                learning_to_content(learning),
            )


# ------------------------------ module-level helpers ------------------------------


def _path_from_call(
    call_name: str, call_arguments: dict[str, Any], call_result: str
) -> str:
    """Extract the relative path a write-flavoured tool touched, if any."""
    if call_name == "write_file":
        return str(call_arguments.get("path", "") or "")
    if call_name == "fetch_url":
        return str(call_arguments.get("save_as", "") or "")
    if call_name in ("edit_file_replace", "edit_file_insert_before", "edit_file_append"):
        return str(call_arguments.get("path", "") or "")
    return ""


def _operation_label(
    call_name: str, call_arguments: dict[str, Any], call_result: str
) -> str:
    """Short human-readable label for the write operation."""
    if call_name == "write_file":
        return "write"
    if call_name == "fetch_url":
        return "fetch:save_as"
    if call_name == "edit_file_replace":
        return "edit:replace"
    if call_name == "edit_file_insert_before":
        return "edit:insert_before"
    if call_name == "edit_file_append":
        return "edit:append"
    return call_name


def build_output_path_banner(output_path: str) -> str:
    """Render the required-output-path block that prefixes task + stage prompts.

    A structured, high-visibility constant the agent can reliably pattern-match
    even when the rest of the prompt is long. Weak models get lost when a path
    is only mentioned once in prose; repeating it in a dedicated block with a
    ``PATH CONSTANT:`` label anchors the right answer.
    """
    return (
        "## REQUIRED OUTPUT PATH\n"
        f"PATH CONSTANT: `{output_path}`\n"
        f"When you call `write_file` (or `fetch_url save_as=...`), the path "
        f"argument MUST be exactly `{output_path}`. Do NOT wrap it with "
        f"extra directories. Do NOT append an extension."
    )


def _assistant_turn(llm: LLMProtocol, resp: LLMResponse) -> dict[str, Any]:
    """Ask the adapter to shape the assistant turn; fall back to Anthropic blocks."""
    formatter = getattr(llm, "format_assistant_turn", None)
    if callable(formatter):
        return formatter(resp)
    blocks: list[dict[str, Any]] = []
    if resp.content:
        blocks.append({"type": "text", "text": resp.content})
    for call in resp.tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        )
    return {"role": "assistant", "content": blocks}


def _tool_results_turn(
    llm: LLMProtocol, calls: list[ToolCall], results: list[str]
) -> dict[str, Any] | list[dict[str, Any]]:
    """Ask the adapter to shape the tool-results message.

    Adapters may return a single dict (Anthropic/Ollama — fold all results
    into one message) or a list of dicts (strict OpenAI-style — one message
    per tool_call_id). Callers must handle both: use :func:`_append_turn`
    to transparently extend ``messages`` regardless of shape.
    """
    formatter = getattr(llm, "format_tool_results", None)
    if callable(formatter):
        return formatter(calls, results)
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": r}
            for c, r in zip(calls, results, strict=True)
        ],
    }


def _append_turn(
    messages: list[dict[str, Any]],
    turn: dict[str, Any] | list[dict[str, Any]],
) -> None:
    """Append a single turn, or extend with a list of turns, to ``messages``.

    Bridges the Anthropic-shape (one dict per turn) and OpenAI-shape (one
    tool message per tool_call_id) adapter conventions without either
    caller having to branch on return type.
    """
    if isinstance(turn, list):
        messages.extend(turn)
    else:
        messages.append(turn)


def _build_stub_awareness_hint(task: Task, work_dir: str) -> str:
    """Detect Sprint 7.5 pre-written stubs and coach the model to fill them.

    When ``seed_workspace`` pre-writes ``src/*.py`` from ``api_spec.md``,
    the implementer's output file already exists with ``raise
    NotImplementedError`` bodies. Weak models see the file, assume the
    task is done, and return zero tool calls — burning their retry
    budget. This hint tells them the file is a stub and points at the
    AST-aware upsert tools.

    Returns an empty string when the task's output_path doesn't point at
    a stub file, so non-implementer tasks see no extra prompting.
    """
    from pathlib import Path as _P

    rel = task.output_path or ""
    if not rel.endswith(".py") or not rel.startswith("src/"):
        return ""
    try:
        path = _P(work_dir) / rel
        if not path.is_file():
            return ""
        body = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if "raise NotImplementedError" not in body:
        return ""
    return (
        "## This file is a pre-generated STUB\n"
        f"The file at `{rel}` was scaffolded from `plan/api_spec.md` and "
        "exists with the correct class/function signatures but every body "
        "is `raise NotImplementedError`. Your job is to REPLACE each "
        "NotImplementedError with a real implementation.\n\n"
        "Use `add_function(path, code)` for top-level functions and "
        "`add_class_method(path, class_name, code)` for class methods — "
        "both tools upsert by name, so calling them with the same name "
        "REPLACES the existing stub body in place.\n\n"
        "Do NOT call write_file on this path; the stub is already there. "
        "Read the file first with read_file if you need to see the exact "
        "signatures.\n"
    )


# v3 tool-boundary contract (findings F1). One hard-coded, tool-specific hint;
# a general hint registry stays speculative until v3 data justifies it.
_TOOL_HINTS: dict[str, str] = {
    "mark_complete": (
        "It does not write files; use write_file(path, content) first, then "
        "mark_complete(summary=...)"
    ),
}


@dataclass(frozen=True)
class CorrectiveError:
    """A tool failure rendered back to the model as actionable correction.

    Renders to a single tool-result message with three parts: what was wrong,
    the tool's expected schema, and an optional hint. Replaces the v2
    crash-as-string (a raw ``KeyError``/traceback), which the autopsy showed
    weak models could not recover from (they re-emitted the same bad call).
    """

    tool_name: str
    problem: str
    schema: dict[str, Any]
    hint: str = ""

    def render(self) -> str:
        parts = [
            f"ERROR: your {self.tool_name} call was rejected: {self.problem}",
            f"Expected schema for {self.tool_name}: "
            f"{json.dumps(self.schema, sort_keys=True)}",
        ]
        if self.hint:
            parts.append(f"Hint: {self.hint}")
        return "\n".join(parts)


def validate_call(
    call: ToolCall, schema: dict[str, Any] | None
) -> CorrectiveError | None:
    """Pure pre-dispatch check: are the call's required arguments present?

    Returns a :class:`CorrectiveError` when a required key from the tool's JSON
    schema is missing, else ``None``. Only required-key presence is checked here
    (the cheap, high-signal case — e.g. mark_complete called with write_file's
    ``path``/``content`` and no ``summary``); deeper type errors surface as
    handler exceptions, which the corrective dispatch also renders.
    """
    if not schema:
        return None
    args = call.arguments or {}
    missing = [k for k in (schema.get("required") or []) if k not in args]
    if not missing:
        return None
    return CorrectiveError(
        tool_name=call.name,
        problem=f"missing required argument(s): {', '.join(missing)}",
        schema=schema,
        hint=_TOOL_HINTS.get(call.name, ""),
    )


#: Byte bound for the S4 near-miss capture.
_ARTIFACT_CAPTURE_LIMIT = 2048


def _capture_failed_artifact(
    output_path: str, success: bool, work_dir: str
) -> dict[str, Any] | None:
    """Capture the bytes actually written to ``output_path`` on a failed task
    where the file exists (the equality near-miss, S4). Returns None on success,
    when the task declared no output, or when nothing was written (a plain
    file_exists failure — no bytes to diff). First 2KB, truncation flagged.
    """
    if success or not output_path:
        return None
    path = Path(work_dir) / output_path
    if not path.is_file():
        return None
    raw = path.read_bytes()
    return {
        "path": output_path,
        "size_bytes": len(raw),
        "truncated": len(raw) > _ARTIFACT_CAPTURE_LIMIT,
        "text": raw[:_ARTIFACT_CAPTURE_LIMIT].decode("utf-8", errors="replace"),
    }


def _expected_output_missing(ctx: ToolContext) -> str | None:
    """Return the task's expected output path if it declared one and no bytes
    have been written there yet, else ``None`` (the completion-nudge trigger, S2).

    This is the "postconditions unmet" proxy the nudge acts on — specifically the
    ``file_exists`` failure the fidelity probe gates on, and the exact condition
    its message names ("expected output X has not been written"). A written-but-
    wrong output (the gemma near-miss) is NOT nudged — the loop already produced
    output; that failure is byte-precision (see S4 artifact_capture), not a dead
    loop. Reuses the same output-written signal as the post-task narration
    redirect, which cannot fire in-loop (it needs the final artifact list).
    """
    rel = ctx.expected_output_path
    if not rel:
        return None
    path = Path(ctx.work_dir) / rel
    if path.is_file() and path.stat().st_size > 0:
        return None
    return rel


async def _run_tool(call: ToolCall, executor: dict[str, Any]) -> str:
    fn = executor.get(call.name)
    if fn is None:
        return f"ERROR: unknown tool {call.name!r}"
    try:
        return await fn(call.arguments)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: tool {call.name} raised: {exc}"


async def _dispatch_tool(
    call: ToolCall,
    executor: dict[str, Any],
    *,
    schema: dict[str, Any] | None,
    mode: str,
) -> str:
    """Execute one tool call under the configured error policy.

    ``mode="raw"`` is byte-identical to v2 (:func:`_run_tool`, crash-as-string
    included). ``mode="corrective"`` validates arguments before dispatch and
    renders any handler exception as a :class:`CorrectiveError`, so a raw
    traceback / bare ``KeyError`` string never reaches the model again.
    """
    if mode != "corrective":
        return await _run_tool(call, executor)
    fn = executor.get(call.name)
    if fn is None:
        return f"ERROR: unknown tool {call.name!r}"
    rejection = validate_call(call, schema)
    if rejection is not None:
        return rejection.render()
    try:
        return await fn(call.arguments)
    except Exception as exc:  # noqa: BLE001
        return CorrectiveError(
            tool_name=call.name,
            problem=f"the call raised while running: {exc}",
            schema=schema or {},
            hint=_TOOL_HINTS.get(call.name, ""),
        ).render()


def _collect_artifacts(ctx: ToolContext) -> list[str]:
    """Union of everything the task produced: explicit mark_complete
    ``artifacts`` plus anything that landed in ``written_files`` via
    write_file / fetch_url. Weak models routinely call ``mark_complete``
    with an empty artifacts list, which used to block the synthesized
    completion's written_files snapshot from ever reaching postcondition
    evaluation. Merging both sources here makes the contract robust to
    that failure mode.
    """
    seen: set[str] = set()
    artifacts: list[str] = []
    for completion in ctx.completions:
        for a in completion.get("artifacts", []):
            if a not in seen:
                seen.add(a)
                artifacts.append(a)
    for rel in ctx.written_files:
        if rel not in seen:
            seen.add(rel)
            artifacts.append(rel)
    return artifacts


def _evaluate_postconditions(
    task: Task, output: str, artifacts: list[str], ctx: ToolContext
) -> list[tuple[str, bool, str]]:
    postcondition_context = {
        "output": output,
        "artifacts": artifacts,
        "completions": ctx.completions,
        "progress_log": ctx.progress_log,
        "work_dir": ctx.work_dir,
        # v2.1: expose the plan-authoring draft so ``plan_draft_*`` predicates
        # in the registry can gate author-stage tasks on the draft's shape.
        # Prefer control.plan_draft (shared across tasks in the run); fall
        # back to ctx.plan_draft for single-task / test contexts.
        "plan_draft": (
            getattr(ctx.control, "plan_draft", None)
            if ctx.control is not None
            else ctx.plan_draft
        ) or ctx.plan_draft,
    }
    failures = dict(evaluate_postconditions(task.spec, postcondition_context))
    results: list[tuple[str, bool, str]] = []
    for pred in task.spec.postconditions:
        if pred.name in failures:
            results.append((pred.name, False, failures[pred.name]))
        else:
            results.append((pred.name, True, ""))
    return results


def _parse_learnings(raw: str, task_ref: TaskId) -> list[Learning]:
    text = raw.strip()
    if not text:
        return []
    # Strip optional code fences.
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:])
    # Find the first JSON array in the blob.
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []

    now = datetime.now(UTC).isoformat()
    learnings: list[Learning] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            category = LearningCategory(item.get("category", "pattern"))
        except ValueError:
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        confidence = float(item.get("confidence", 0.5) or 0.5)
        confidence = max(0.0, min(1.0, confidence))
        learnings.append(
            Learning(
                id=str(uuid.uuid4()),
                category=category,
                content=content,
                confidence=confidence,
                task_ref=task_ref,
                created_at=now,
            )
        )
    return learnings


def _merge_usage(total: dict[str, Any], incr: dict[str, Any]) -> None:
    """Accumulate token counts (ints) and cost (float, USD) across turns.

    LiteLLM adapters populate ``cost_usd`` per response; local/direct
    adapters only populate tokens. Floats are kept as floats so fractional
    cents aren't truncated; everything else coerces to int.
    """
    for key, value in incr.items():
        prior = total.get(key, 0)
        if isinstance(value, float) or isinstance(prior, float):
            total[key] = float(prior) + float(value)
        else:
            total[key] = int(prior) + int(value)
