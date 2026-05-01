"""Synthesize learnings automatically from postcondition failures.

The flywheel's agent-volunteered path (``report_learning`` tool) only works when
the agent actually calls it. In practice the model often bails on a task with
``tool_calls=0`` at the end — so the most instructive failures (where a
postcondition caught a real bug) record nothing, and the loopback retry starts
from a blank slate. This module closes that gap.

Given a task id, a failed predicate name, and the predicate's reason string, it
builds a deterministic :class:`~agora.core.learning.Learning` that the
orchestrator can post to Matrix and re-inject into the retry's context. No
agent cooperation required.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from agora.core.learning import Learning
from agora.core.types import LearningCategory, TaskId

AUTO_LEARNING_CONFIDENCE = 0.8
"""Higher than agent reflections because postconditions are ground truth."""

AUTO_LEARNING_MARKER = "[auto]"
"""Prefix so post-hoc analytics can distinguish auto vs agent-volunteered."""


def synthesize_failure_learning(
    *,
    task_id: TaskId,
    predicate_name: str,
    reason: str,
    now: datetime | None = None,
) -> Learning:
    """Build a deterministic Learning from one postcondition failure.

    The learning id is a stable hash of ``(task_id, predicate_name, reason)`` so
    the same failure recurring twice reinforces the original rather than
    duplicating. The category is always :attr:`LearningCategory.FAILURE`.
    """
    timestamp = (now or datetime.now(UTC)).isoformat()
    content = _format_content(task_id, predicate_name, reason)
    digest = hashlib.sha256(
        f"{task_id}|{predicate_name}|{_normalise_reason(reason)}".encode()
    ).hexdigest()[:16]
    return Learning(
        id=f"auto-{digest}",
        category=LearningCategory.FAILURE,
        content=content,
        confidence=AUTO_LEARNING_CONFIDENCE,
        task_ref=task_id,
        reinforcement_count=0,
        created_at=timestamp,
        last_reinforced_at=timestamp,
    )


def _format_content(task_id: TaskId, predicate_name: str, reason: str) -> str:
    reason_snippet = _shorten(reason, 500)
    return (
        f"{AUTO_LEARNING_MARKER} task `{task_id}` failed postcondition "
        f"`{predicate_name}`: {reason_snippet}"
    )


def _normalise_reason(reason: str) -> str:
    """Collapse whitespace and strip volatile path/line noise for fingerprinting.

    Two runs that fail the same way on different tmp paths should dedup to the
    same learning id.
    """
    text = reason.strip().lower()
    text = re.sub(r"\s+", " ", text)
    # Windows absolute paths: `c:\users\...` — regex `\\` matches one literal backslash.
    text = re.sub(r"['\"]?[a-z]:\\[^'\"\s]+", "<path>", text)
    # POSIX absolute paths: `/tmp/...`.
    text = re.sub(r"['\"]?/[^'\"\s]+", "<path>", text)
    return text[:400]


def _shorten(text: str, limit: int) -> str:
    compact = text.strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


__all__ = [
    "AUTO_LEARNING_CONFIDENCE",
    "AUTO_LEARNING_MARKER",
    "synthesize_failure_learning",
]
