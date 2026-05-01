"""Knowledge flywheel — structured learnings with confidence decay."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from agora.core.types import LearningCategory, TaskId

CONFIDENCE_THRESHOLD = 0.3
DECAY_RATE = 0.1
MAX_CONFIDENCE = 1.0
REINFORCE_BOOST = 0.15


@dataclass(frozen=True)
class Learning:
    id: str
    category: LearningCategory
    content: str
    confidence: float
    task_ref: TaskId
    reinforcement_count: int = 0
    created_at: str = ""
    last_reinforced_at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
