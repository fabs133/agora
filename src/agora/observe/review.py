"""Human-in-the-loop review coordinator.

When the orchestrator enters the REVIEW phase, it calls
:meth:`ReviewCoordinator.request_review`. The coordinator:

1. Posts a review summary to the project room.
2. Posts an MSC3381 poll with pre-labelled options.
3. Subscribes (one-shot) to poll responses targeting this poll and to
   ``/agora review <answer_id>`` fallback commands.
4. Waits up to ``review_timeout_seconds`` for a vote.
5. Builds a :class:`ReviewDecision` from the answer id + any free-text
   feedback from replies threaded under the poll message.
6. On timeout, falls back to the headless auto-review.

The coordinator never blocks the event loop: all waiting happens on
:class:`asyncio.Event` / :class:`asyncio.Future`, and the timeout is bounded
by :func:`asyncio.wait_for`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agora.core.project import Project
from agora.core.types import ProjectPhase, RoomId
from agora.fleet.orchestrator import ReviewDecision
from agora.matrix.client import MatrixClientProtocol
from agora.matrix.sync import EventDispatcher
from agora.observe.commands import VERB_REVIEW, ParsedCommand
from agora.observe.formatters import (
    format_ack,
    format_error,
    format_review_summary,
)
from agora.observe.polls import (
    ANSWER_APPROVE,
    ANSWER_REJECT_ANALYSIS,
    ANSWER_REJECT_ARCHITECTURE,
    ANSWER_REJECT_IMPLEMENTATION,
    ANSWER_REJECT_TESTING,
    PollResponse,
    build_review_poll,
    review_poll_event_type,
)

logger = logging.getLogger(__name__)


_ANSWER_TO_PHASE: dict[str, ProjectPhase | None] = {
    ANSWER_APPROVE: None,
    ANSWER_REJECT_ANALYSIS: ProjectPhase.ANALYSIS,
    ANSWER_REJECT_ARCHITECTURE: ProjectPhase.ARCHITECTURE,
    ANSWER_REJECT_IMPLEMENTATION: ProjectPhase.IMPLEMENTATION,
    ANSWER_REJECT_TESTING: ProjectPhase.TESTING,
    # Ergonomic shorthands — the typed-reject ecosystem is obscure and most
    # review failures happen in IMPLEMENTATION so that's the safe default.
    "reject": ProjectPhase.IMPLEMENTATION,
    "retry": ProjectPhase.IMPLEMENTATION,
    "loopback": ProjectPhase.IMPLEMENTATION,
    "ok": None,
    "yes": None,
}


class ReviewCoordinator:
    """Drives the REVIEW-phase poll/approval loop on the Matrix observer side.

    Composed by the orchestrator and called once per project entering
    REVIEW. The coordinator owns the lifecycle of a single MSC3381 poll
    event plus its associated ``/agora review`` fallback command path,
    routes both into one ``asyncio.Future``, and resolves to a
    :class:`~agora.fleet.orchestrator.ReviewDecision`.

    Free-text feedback in threaded replies under the poll message is
    captured and surfaced on the decision so the next agent's prompt can
    incorporate the human's reasoning when looping back.
    """

    def __init__(
        self,
        matrix_client: MatrixClientProtocol,
        dispatcher: EventDispatcher,
        project_room_id: RoomId,
        timeout_seconds: float = 86400.0,
        project_work_dir: str | None = None,
        control: Any | None = None,
    ) -> None:
        self._client = matrix_client
        self._dispatcher = dispatcher
        self._room = project_room_id
        self._timeout = timeout_seconds
        self._project_work_dir = project_work_dir
        self._control = control

        self._pending_poll_event_id: str | None = None
        self._decision_future: asyncio.Future[tuple[str, str]] | None = None
        self._attached = False

    def attach(self) -> None:
        """Register poll and command handlers on the event dispatcher.

        Idempotent — calling twice on the same coordinator is a no-op.
        Normally invoked implicitly by :meth:`request_review`; expose it
        directly only if you need handlers wired before the first review
        poll is posted (e.g. integration tests that pre-feed events).
        """
        if self._attached:
            return
        self._dispatcher.on_poll_response(self._on_poll_response)
        self._dispatcher.on_command(self._on_command)
        self._attached = True

    async def request_review(
        self,
        project: Project,
        results_summary: list[dict[str, Any]],
    ) -> ReviewDecision:
        """Post summary + poll, then await a vote (with timeout)."""
        self.attach()

        # 1. Review summary (with on-disk artifact snapshot if we know where to look)
        reactions = (
            getattr(self._control, "task_reactions", None) if self._control else None
        )
        snapshot = _gather_artifact_snapshot(
            self._project_work_dir, results_summary, reactions=reactions
        )
        summary = format_review_summary(
            project.name, project.phase, results_summary, artifact=snapshot
        )
        await self._client.send_event(self._room, "m.room.message", summary.to_content())

        # 2. Poll
        poll_content = build_review_poll(project.name, project.phase)
        poll_event_id = await self._client.send_event(
            self._room, review_poll_event_type(), poll_content
        )

        # 3. Wait for a vote.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, str]] = loop.create_future()
        self._pending_poll_event_id = poll_event_id
        self._decision_future = future

        try:
            answer_id, voter = await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            logger.info(
                "review timed out after %.0fs for project %s — auto-rejecting any failures",
                self._timeout,
                project.name,
            )
            return await _auto_fallback(results_summary)
        finally:
            self._pending_poll_event_id = None
            self._decision_future = None

        # 4. Build decision + acknowledge.
        decision = self._build_decision(answer_id, voter)
        await self._client.send_event(
            self._room,
            "m.room.message",
            format_ack(
                f"review vote recorded: {answer_id} "
                f"(by {voter or 'unknown'})"
            ).to_content(),
        )
        return decision

    # ---- handlers ---------------------------------------------------------

    async def _on_poll_response(self, room_id: RoomId, response: PollResponse) -> None:
        if room_id != self._room:
            return
        if self._pending_poll_event_id is None or self._decision_future is None:
            return
        if response.poll_event_id != self._pending_poll_event_id:
            return
        if response.answer_id not in _ANSWER_TO_PHASE:
            await self._client.send_event(
                self._room,
                "m.room.message",
                format_error(f"unknown review answer: {response.answer_id}").to_content(),
            )
            return
        if not self._decision_future.done():
            self._decision_future.set_result((response.answer_id, response.user_id))

    async def _on_command(self, room_id: RoomId, cmd: ParsedCommand) -> None:
        if room_id != self._room or cmd.verb != VERB_REVIEW:
            return
        if self._pending_poll_event_id is None or self._decision_future is None:
            return
        if len(cmd.args) != 1:
            await self._client.send_event(
                self._room,
                "m.room.message",
                format_error("/agora review requires exactly one <answer_id>").to_content(),
            )
            return
        answer_id = cmd.args[0]
        if answer_id not in _ANSWER_TO_PHASE:
            await self._client.send_event(
                self._room,
                "m.room.message",
                format_error(f"unknown review answer: {answer_id}").to_content(),
            )
            return
        if not self._decision_future.done():
            self._decision_future.set_result((answer_id, cmd.sender))

    # ---- helpers ----------------------------------------------------------

    def _build_decision(self, answer_id: str, voter: str) -> ReviewDecision:
        target_phase = _ANSWER_TO_PHASE[answer_id]
        if target_phase is None:
            return ReviewDecision(
                approved=True,
                feedback=f"approved by {voter}" if voter else "approved",
            )
        return ReviewDecision(
            approved=False,
            feedback=f"rejected by {voter} → {target_phase.value}",
            return_to_phase=target_phase,
        )


def _success(row: Any) -> bool:
    if isinstance(row, dict):
        return bool(row.get("success"))
    return bool(getattr(row, "success", False))


def _gather_artifact_snapshot(
    work_dir: str | None,
    results_summary: list[Any],
    reactions: dict[str, list[tuple[str, str]]] | None = None,
):
    """Collect file tree + recent commits + failed postconditions + reaction counts for the review.

    Returns an ``ArtifactSnapshot`` always; ``files`` / ``recent_commits`` are
    empty when no ``work_dir`` is configured (e.g. MCP flow).

    ``reactions`` maps ``task_id -> [(sender, key), ...]`` — typically sourced
    from :attr:`OrchestratorControl.task_reactions`.
    """
    from agora.observe.formatters import ArtifactSnapshot

    failures: list[tuple[str, str, str]] = []
    for r in results_summary:
        if _success(r):
            continue
        task_id = (
            r.get("task_id", "") if isinstance(r, dict) else getattr(r, "task_id", "")
        )
        post = (
            r.get("postcondition_results")
            if isinstance(r, dict)
            else getattr(r, "postcondition_results", None)
        ) or []
        for row in post:
            try:
                name, passed, reason = row
            except (TypeError, ValueError):
                continue
            if not passed:
                failures.append((str(task_id), str(name), str(reason)))

    reaction_counts: dict[str, dict[str, int]] = {}
    for task_id, entries in (reactions or {}).items():
        counts: dict[str, int] = {}
        for _sender, key in entries:
            counts[key] = counts.get(key, 0) + 1
        if counts:
            reaction_counts[str(task_id)] = counts

    if not work_dir:
        return ArtifactSnapshot(
            files=[],
            recent_commits=[],
            postcondition_failures=failures,
            reaction_counts=reaction_counts,
        )

    files: list[tuple[str, int]] = []
    try:
        from pathlib import Path

        root = Path(work_dir)
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                # Skip the git internals and python bytecode caches.
                rel_parts = p.relative_to(root).parts
                if not rel_parts:
                    continue
                if rel_parts[0] in {".git", "__pycache__"}:
                    continue
                if any(part == "__pycache__" for part in rel_parts):
                    continue
                if p.is_file():
                    try:
                        size = p.stat().st_size
                    except OSError:
                        size = 0
                    files.append(("/".join(rel_parts), size))
    except Exception as exc:  # noqa: BLE001
        logger.warning("review: file-tree scan failed in %s: %s", work_dir, exc)

    commits: list[str] = []
    try:
        import subprocess

        result = subprocess.run(
            ["git", "log", "--oneline", "-15"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            commits = [
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip()
            ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("review: git log failed in %s: %s", work_dir, exc)

    return ArtifactSnapshot(
        files=files,
        recent_commits=commits,
        postcondition_failures=failures,
        reaction_counts=reaction_counts,
    )


async def _auto_fallback(results_summary: list[Any]) -> ReviewDecision:
    failed = [r for r in results_summary if not _success(r)]
    if not failed:
        return ReviewDecision(approved=True, feedback="auto-approved after review timeout")
    return ReviewDecision(
        approved=False,
        feedback=f"auto-rejected after timeout: {len(failed)} task(s) failed",
        return_to_phase=ProjectPhase.IMPLEMENTATION,
    )


__all__ = ["ReviewCoordinator"]
