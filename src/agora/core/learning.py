"""Knowledge flywheel — structured learnings with confidence decay.

Position: the retry-memory layer. When a task fails a postcondition the runtime
records a :class:`Learning` (a failure trace + a corrective hint) and injects it
into the model's next-turn prompt — the mechanism that turns a raw failure into
context on retry, rather than letting the model repeat it blind.

Invariants: confidence is bounded to ``[0, MAX_CONFIDENCE]``; a learning below
``CONFIDENCE_THRESHOLD`` is considered stale and dropped; reinforcement adds
``REINFORCE_BOOST`` and time subtracts ``DECAY_RATE`` — so a hint that keeps
proving useful survives and one that stops mattering ages out on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from agora.core.types import LearningCategory, TaskId

CONFIDENCE_THRESHOLD = 0.3
DECAY_RATE = 0.1
MAX_CONFIDENCE = 1.0
REINFORCE_BOOST = 0.15


@dataclass(frozen=True)
class Learning:
    """A failure trace lifted from a postcondition into the next agent prompt.

    Learnings are the framework's loopback mechanism: when a task fails a
    postcondition, the orchestrator synthesises a ``Learning`` from the
    ``(task_id, predicate_name, reason)`` tuple via
    :func:`agora.fleet.auto_learning.synthesize_failure_learning` and injects
    it into the agent's system prompt for the retry. ``confidence`` is
    boosted by :func:`reinforce` when the same failure recurs and decays
    over time via :func:`decay_learnings`; only learnings with confidence
    above ``CONFIDENCE_THRESHOLD`` (0.3) are surfaced to the model.
    """

    id: str
    category: LearningCategory
    content: str
    confidence: float
    task_ref: TaskId
    reinforcement_count: int = 0
    created_at: str = ""
    last_reinforced_at: str = ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def decay_learnings(learnings: list[Learning]) -> list[Learning]:
    """Apply confidence decay uniformly. Floor at 0.0."""
    out: list[Learning] = []
    for learning in learnings:
        new_conf = max(0.0, learning.confidence - DECAY_RATE)
        out.append(replace(learning, confidence=new_conf))
    return out


def filter_active(learnings: list[Learning]) -> list[Learning]:
    """Learnings above the confidence threshold, sorted descending by confidence."""
    active = [l for l in learnings if l.confidence >= CONFIDENCE_THRESHOLD]
    return sorted(active, key=lambda l: l.confidence, reverse=True)


def reinforce(learning: Learning) -> Learning:
    """Boost confidence when a learning is referenced again; cap at MAX_CONFIDENCE."""
    new_conf = min(MAX_CONFIDENCE, learning.confidence + REINFORCE_BOOST)
    return replace(
        learning,
        confidence=new_conf,
        reinforcement_count=learning.reinforcement_count + 1,
        last_reinforced_at=_now_iso(),
    )


def format_learnings_for_context(learnings: list[Learning]) -> str:
    """Format learnings as a prompt context block, grouped by category."""
    if not learnings:
        return ""
    grouped: dict[LearningCategory, list[Learning]] = {}
    for learning in learnings:
        grouped.setdefault(learning.category, []).append(learning)

    lines: list[str] = ["## Learned context"]
    for category in LearningCategory:
        items = grouped.get(category)
        if not items:
            continue
        lines.append(f"\n### {category.value}")
        for l in sorted(items, key=lambda x: x.confidence, reverse=True):
            lines.append(f"- ({l.confidence:.2f}) {l.content}")
    return "\n".join(lines)
