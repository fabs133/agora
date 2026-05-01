"""Per-project orchestrator control: pause/abort/redirect/note plumbing.

:class:`OrchestratorControl` is constructed once per ``run_project`` call and:

- Registered on the project's :class:`~agora.matrix.sync.EventDispatcher` as the
  ``/agora`` command handler (scoped to the project room).
- Awaited at task-dispatch boundaries inside ``_run_phase`` so pause/abort can
  take effect between tasks (never mid-LLM-call).
- Passed into :class:`~agora.fleet.inner_tools.ToolContext` so agent runtimes
  consume fresh observer notes + any pending redirect at the start of each turn.

The only mutable state is a few ``asyncio`` primitives plus two dicts; all
mutations happen on the same event loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agora.core.errors import AgoraError
from agora.core.types import RoomId
from agora.matrix.client import MatrixClientProtocol
from agora.observe import formatters
from agora.observe.commands import (
    VERB_ABORT,
    VERB_COMMENT,
    VERB_DECISION,
    VERB_HELP,
    VERB_NOTE,
    VERB_PAUSE,
    VERB_REDIRECT,
    VERB_RESUME,
    ParsedCommand,
    validate,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class AbortedError(AgoraError):
    """Raised when `/agora abort` fires — transitions the project to FAILED."""


@dataclass
class OrchestratorControl:
    project_room_id: RoomId
    matrix_client: MatrixClientProtocol
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    notes: list[str] = field(default_factory=list)
    agent_redirects: dict[str, str] = field(default_factory=dict)
    task_comments: dict[str, list[str]] = field(default_factory=dict)
    #: event_id (of a write-event card we posted) → task_id it belongs to.
    #: Lets :meth:`handle_reaction` and :meth:`handle_reply` resolve a reactor's
    #: target back to the task it affects.
    task_card_events: dict[str, str] = field(default_factory=dict)
    #: ``task_id -> [(sender, emoji_key), ...]`` aggregate of all reactions on
    #: task cards belonging to that task. Surfaced at REVIEW time so the
    #: reviewer sees quick signal without scrolling.
    task_reactions: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    #: ``decision_id → asyncio.Future`` — the primitive the ``await_user_decision``
    #: inner tool blocks on. Resolved by :meth:`resolve_decision` when a poll
    #: response with a matching ``POLL_DECISION_ID_KEY`` arrives.
    pending_decisions: dict[str, asyncio.Future[str]] = field(default_factory=dict)
    #: ``decision_id → answer_id`` — populated as decisions resolve, kept as a
    #: record of what the user chose (useful for debugging and for late-arriving
    #: tool calls that raced the poll response).
    decision_responses: dict[str, str] = field(default_factory=dict)
    #: ``poll_event_id → decision_id`` — lookup so the dispatcher can route a
    #: poll response event (which only knows the poll event id) to the right
    #: decision future. Populated by :meth:`register_decision_poll`.
    poll_event_to_decision: dict[str, str] = field(default_factory=dict)
    #: ``message_event_id → (decision_id, {emoji_key → answer_id})`` — populated
    #: by :meth:`register_decision_reactions` when a decision-stage posts its
    #: question card. When a reaction arrives on a registered message, the
    #: mapped answer resolves the decision. This is the Element-native
    #: fallback when MSC3381 poll widgets don't render for a user's client.
    decision_reaction_map: dict[str, tuple[str, dict[str, str]]] = field(
        default_factory=dict
    )
    #: Mutable plan-authoring draft shared across ALL tasks in a single
    #: ``run_project``. Lives on ``OrchestratorControl`` rather than
    #: ``ToolContext`` because a fresh ToolContext is constructed per task
    #: attempt, so the draft would otherwise be lost between the ``author_*``
    #: stages and the ``finalize_plan`` stage. The plan-authoring tools in
    #: :mod:`agora.fleet.inner_tools` resolve to this shared instance via
    #: the per-task ``ctx.control`` attribute.
    plan_draft: Any = None
    abort_reason: str = ""

    def __post_init__(self) -> None:
        # Default: running (not paused).
        self.pause_event.set()

    # --------------------------------------------------------------- observer API

    async def handle_command(self, room_id: RoomId, cmd: ParsedCommand) -> None:
        """Dispatcher callback. Ignores commands aimed at other rooms or `/agora review`."""
        if room_id != self.project_room_id:
            return

        # Validate early so malformed inputs get a usage message instead of a crash.
        ok, reason = validate(cmd)

        if cmd.verb == VERB_HELP:
            await self._ack(formatters.format_help())
            return

        if not ok:
            await self._ack(formatters.format_error(reason))
            return

        if cmd.verb == VERB_NOTE:
            self.notes.append(cmd.argline)
            await self._ack(formatters.format_note(cmd.sender or "observer", cmd.argline))
            return

        if cmd.verb == VERB_PAUSE:
            self.pause_event.clear()
            await self._ack(formatters.format_ack("⏸ paused — no new tasks will dispatch"))
            return

        if cmd.verb == VERB_RESUME:
            self.pause_event.set()
            await self._ack(formatters.format_ack("▶ resumed"))
            return

        if cmd.verb == VERB_ABORT:
            reason_text = cmd.argline or "aborted by observer"
            self.abort_reason = reason_text
            self.abort_event.set()
            # If paused, wake the gate so wait_unpaused can observe the abort.
            self.pause_event.set()
            await self._ack(formatters.format_ack(f"⏹ aborting: {reason_text}"))
            return

        if cmd.verb == VERB_REDIRECT:
            agent_name = cmd.args[0]
            new_instructions = " ".join(cmd.args[1:])
            self.agent_redirects[agent_name] = new_instructions
            await self._ack(
                formatters.format_ack(f"redirect queued for agent {agent_name!r}")
            )
            return

        if cmd.verb == VERB_COMMENT:
            task_id = cmd.args[0]
            text = " ".join(cmd.args[1:])
            self.task_comments.setdefault(task_id, []).append(text)
            await self._ack(
                formatters.format_ack(
                    f"comment queued for task {task_id!r}: {text[:80]}"
                )
            )
            return

        if cmd.verb == VERB_DECISION:
            # Two forms: `/agora decision <answer>` (resolves the single
            # pending decision) or `/agora decision <decision_id> <answer>`
            # (explicit). Fallback for clients where MSC3381 polls don't render.
            if len(cmd.args) == 1:
                answer_id = cmd.args[0]
                pending_ids = [
                    d for d, f in self.pending_decisions.items() if not f.done()
                ]
                if not pending_ids:
                    await self._ack(
                        formatters.format_error("no pending decision to resolve")
                    )
                    return
                if len(pending_ids) > 1:
                    await self._ack(
                        formatters.format_error(
                            f"multiple pending decisions: {pending_ids}; "
                            f"use `/agora decision <id> <answer>`"
                        )
                    )
                    return
                decision_id = pending_ids[0]
            else:
                decision_id, answer_id = cmd.args[0], cmd.args[1]
            resolved = self.resolve_decision(decision_id, answer_id)
            if resolved:
                await self._ack(
                    formatters.format_ack(
                        f"decision {decision_id!r} resolved: {answer_id}"
                    )
                )
            else:
                await self._ack(
                    formatters.format_error(
                        f"decision {decision_id!r} could not be resolved "
                        f"(unknown id or already resolved)"
                    )
                )
            return

        # /agora review is intentionally ignored here — ReviewCoordinator owns it.

    # ------------------------------------------------------------- orchestrator API

    async def wait_unpaused(self) -> None:
        """Await at task-dispatch boundaries. Blocks while paused."""
        await self.pause_event.wait()

    def is_aborted(self) -> bool:
        return self.abort_event.is_set()

    def raise_if_aborted(self) -> None:
        if self.is_aborted():
            raise AbortedError(self.abort_reason or "aborted")

    # ---------------------------------------------------------- runtime-facing API

    def consume_notes(self) -> list[str]:
        """Take a snapshot of pending notes. Notes are kept so later turns still see them."""
        return list(self.notes)

    def consume_redirect(self, agent_name: str) -> str | None:
        """Pop a pending redirect for ``agent_name`` (one-shot)."""
        return self.agent_redirects.pop(agent_name, None)

    def consume_task_comments(self, task_id: str) -> list[str]:
        """Drain and return any queued comments for ``task_id``.

        Comments are consumed one-shot — they're injected into the next task
        attempt's system prompt and then cleared. On loopback the reviewer can
        queue new comments based on what they observed.
        """
        return self.task_comments.pop(task_id, [])

    # ---------------------------------------------------- interactive surfaces

    def register_task_card(self, event_id: str, task_id: str) -> None:
        """Remember the event_id a write-event card was posted under, so later
        reactions / replies to that card can be resolved to ``task_id``.
        """
        if event_id and task_id:
            self.task_card_events[event_id] = task_id

    def resolve_task_from_event(self, event_id: str) -> str | None:
        """Which task a given card event belongs to, if known."""
        return self.task_card_events.get(event_id)

    async def handle_reaction(
        self, room_id: RoomId, reaction: Any
    ) -> None:
        """EventDispatcher callback. Two routing branches:

        1. If the reaction targets a registered decision-question card, map
           the emoji to an answer and resolve the decision future.
        2. Otherwise, if the target is a task-card, record the reaction in
           ``task_reactions`` for later review-summary rendering.

        A reaction can only refer to one prior event, so the branches don't
        collide — the same dispatcher feeds both code paths unambiguously.
        """
        if room_id != self.project_room_id:
            return

        # Branch 1: decision-question cards.
        decision_entry = self.decision_reaction_map.get(reaction.target_event_id)
        if decision_entry is not None:
            decision_id, emoji_to_answer = decision_entry
            # Normalize the reaction key — Element may strip variation-selector-16
            # from emoji like 1️⃣. Try both forms so voting is robust across clients.
            key = reaction.key
            key_alt = key.replace("\ufe0f", "") if "\ufe0f" in key else key + "\ufe0f"
            answer_id = emoji_to_answer.get(key) or emoji_to_answer.get(key_alt)
            if answer_id is None:
                logger.info(
                    "decision reaction ignored (unmapped emoji): decision=%s key=%r",
                    decision_id, reaction.key,
                )
                return
            resolved = self.resolve_decision(decision_id, answer_id)
            logger.info(
                "decision reaction: decision=%s sender=%s key=%r answer=%s resolved=%s",
                decision_id, reaction.sender, reaction.key, answer_id, resolved,
            )
            return

        # Branch 2: task-card reactions (pre-existing behaviour).
        task_id = self.resolve_task_from_event(reaction.target_event_id)
        if task_id is None:
            # Unrelated reaction (a user reacting to a phase banner, say).
            return
        self.task_reactions.setdefault(task_id, []).append(
            (reaction.sender, reaction.key)
        )
        logger.info(
            "reaction: task=%s sender=%s key=%s",
            task_id, reaction.sender, reaction.key,
        )

    async def handle_reply(
        self, room_id: RoomId, reply: Any
    ) -> None:
        """EventDispatcher callback. When the reply targets a known task card,
        route its body as an implicit ``/agora comment`` for that task."""
        if room_id != self.project_room_id:
            return
        task_id = self.resolve_task_from_event(reply.target_event_id)
        if task_id is None:
            return
        text = reply.body.strip()
        if not text:
            return
        self.task_comments.setdefault(task_id, []).append(text)
        try:
            await self._ack(
                formatters.format_ack(
                    f"comment linked to task {task_id!r}: {text[:80]}"
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reply-ack failed: %s", exc)

    # ---------------------------------------------------- decision polls (planner)

    def register_decision_reactions(
        self,
        decision_id: str,
        message_event_id: str,
        emoji_to_answer: dict[str, str],
    ) -> None:
        """Map a plain-message question card to its decision + emoji→answer table.

        Called by decision stages right after posting the question so a user
        reacting with one of the registered emojis will resolve the pending
        future via :meth:`handle_reaction`.
        """
        if message_event_id and decision_id:
            self.decision_reaction_map[message_event_id] = (
                decision_id,
                dict(emoji_to_answer),
            )

    def register_decision_poll(self, decision_id: str, poll_event_id: str) -> None:
        """Record the ``poll_event_id → decision_id`` mapping so a later
        ``m.poll.response`` can be routed to the right waiting future.

        The caller (typically the ``await_user_decision`` tool) must have
        already placed a Future in ``pending_decisions[decision_id]`` before
        calling this method.
        """
        if decision_id and poll_event_id:
            self.poll_event_to_decision[poll_event_id] = decision_id

    def resolve_decision(self, decision_id: str, answer_id: str) -> bool:
        """Complete the Future for ``decision_id`` with ``answer_id``.

        Returns ``True`` if a waiting future was actually resolved; ``False``
        if the decision was already resolved or unknown (common if the user
        clicks twice). Always records the answer in ``decision_responses``.
        """
        if not decision_id:
            return False
        self.decision_responses[decision_id] = answer_id
        future = self.pending_decisions.get(decision_id)
        if future is None or future.done():
            return False
        future.set_result(answer_id)
        return True

    def decision_id_for_poll(self, poll_event_id: str) -> str | None:
        """Reverse lookup used by the sync dispatcher."""
        return self.poll_event_to_decision.get(poll_event_id)

    async def await_decision(
        self, decision_id: str, timeout_seconds: float = 300.0
    ) -> str:
        """Block until the user resolves the decision. Creates the Future lazily.

        If the decision was already resolved (e.g. the user clicked before the
        tool wired its future), returns the cached answer immediately. Raises
        :class:`asyncio.TimeoutError` if no response arrives within
        ``timeout_seconds``.
        """
        # Already-resolved fast path.
        cached = self.decision_responses.get(decision_id)
        if cached is not None:
            return cached

        future = self.pending_decisions.get(decision_id)
        if future is None:
            future = asyncio.get_event_loop().create_future()
            self.pending_decisions[decision_id] = future

        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        finally:
            # Once awaited (resolved or timed-out), drop the future; leaving
            # it in the dict would mask future polls with the same id.
            self.pending_decisions.pop(decision_id, None)

    # ------------------------------------------------------------------- helpers

    async def _ack(self, message: formatters.FormattedMessage) -> None:
        try:
            await self.matrix_client.send_event(
                self.project_room_id, "m.room.message", message.to_content()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("control ack failed for %s: %s", self.project_room_id, exc)


__all__ = ["AbortedError", "OrchestratorControl"]
