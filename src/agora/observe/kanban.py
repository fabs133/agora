"""Kanban derivation — pure function over a Matrix event stream.

Folds ``m.agora.task`` and ``m.agora.task_result`` events into a snapshot keyed
by task id. Each task's *latest* status event wins; task_results flip the
status to DONE/FAILED.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from agora.core.types import TaskStatus
from agora.matrix.events import (
    TASK_EVENT,
    TASK_RESULT_EVENT,
    task_from_content,
    task_result_from_content,
)


@dataclass
class TaskCard:
    id: str
    description: str
    status: TaskStatus
    agent_id: str | None
    fingerprint: str
    last_timestamp: str = ""


@dataclass
class KanbanBoard:
    columns: dict[TaskStatus, list[TaskCard]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            status.value: [
                {
                    "id": card.id,
                    "description": card.description,
                    "status": card.status.value,
                    "agent_id": card.agent_id,
                    "fingerprint": card.fingerprint,
                    "last_timestamp": card.last_timestamp,
                }
                for card in cards
            ]
            for status, cards in self.columns.items()
        }


def build_kanban(events: Iterable[dict[str, Any]]) -> KanbanBoard:
    """Fold task-related events into a kanban board. Latest event per task wins."""
    by_id: dict[str, TaskCard] = {}

    for event in events:
        etype = event.get("type")
        content = event.get("content") or {}
        try:
            if etype == TASK_EVENT:
                parsed = task_from_content(content)
                tid = parsed["task_id"]
                by_id[tid] = TaskCard(
                    id=tid,
                    description=parsed.get("description", ""),
                    status=parsed["status"],
                    agent_id=parsed.get("agent_id"),
                    fingerprint=parsed.get("fingerprint", ""),
                    last_timestamp=parsed.get("timestamp", ""),
                )
            elif etype == TASK_RESULT_EVENT:
                parsed = task_result_from_content(content)
                tid = parsed["task_id"]
                existing = by_id.get(tid)
                new_status = TaskStatus.DONE if parsed["success"] else TaskStatus.FAILED
                if existing is None:
                    by_id[tid] = TaskCard(
                        id=tid,
                        description="",
                        status=new_status,
                        agent_id=None,
                        fingerprint="",
                        last_timestamp=parsed.get("timestamp", ""),
                    )
                else:
                    existing.status = new_status
                    existing.last_timestamp = parsed.get("timestamp", "") or existing.last_timestamp
        except Exception:  # noqa: BLE001 — malformed events are skipped
            continue

    columns: dict[TaskStatus, list[TaskCard]] = {status: [] for status in TaskStatus}
    for card in by_id.values():
        columns[card.status].append(card)
    # Deterministic ordering inside each column.
    for cards in columns.values():
        cards.sort(key=lambda c: (c.last_timestamp, c.id))
    return KanbanBoard(columns=columns)
