"""Chronological timeline derived from the Matrix event stream.

Position: one of the read-side projections over the observer's Matrix events
(alongside :mod:`kanban` and :mod:`export`). Pure: folds the append-only event
log into an ordered list of :class:`TimelineEntry`, categorised by event kind,
for the HTML report and audit views. No IO, no Matrix client — events in,
entries out.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from agora.matrix.events import (
    LEARNING_EVENT,
    PHASE_CHANGE_EVENT,
    TASK_EVENT,
    TASK_RESULT_EVENT,
    is_agora_event,
)


@dataclass(frozen=True)
class TimelineEntry:
    """One dated row of the timeline: an event reduced to a ``category`` bucket
    and a one-line ``summary``, keeping ``event_id``/``sender`` for provenance so
    a rendered entry links back to its source event."""

    timestamp: str
    room_id: str
    category: str       # "phase" | "task" | "result" | "learning" | "other"
    summary: str
    event_id: str = ""
    sender: str = ""


_CATEGORY_BY_TYPE: dict[str, str] = {
    PHASE_CHANGE_EVENT: "phase",
    TASK_EVENT: "task",
    TASK_RESULT_EVENT: "result",
    LEARNING_EVENT: "learning",
}


def build_timeline(
    events: Iterable[tuple[str, dict[str, Any]]],
    *,
    include_non_agora: bool = False,
) -> list[TimelineEntry]:
    """Fold a stream of ``(room_id, event_dict)`` into ordered TimelineEntry rows."""
    entries: list[TimelineEntry] = []
    for room_id, event in events:
        etype = event.get("type", "")
        if not is_agora_event(etype) and not include_non_agora:
            continue
        content = event.get("content") or {}
        category = _CATEGORY_BY_TYPE.get(etype, "other")
        entries.append(
            TimelineEntry(
                timestamp=_extract_timestamp(content, event),
                room_id=room_id,
                category=category,
                summary=_summarize(etype, content),
                event_id=str(event.get("event_id", "")),
                sender=str(event.get("sender", "")),
            )
        )
    entries.sort(key=lambda e: (e.timestamp, e.event_id))
    return entries


def _extract_timestamp(content: dict[str, Any], event: dict[str, Any]) -> str:
    ts = content.get("timestamp")
    if isinstance(ts, str) and ts:
        return ts
    origin = event.get("origin_server_ts")
    if isinstance(origin, int):
        from datetime import datetime

        return datetime.fromtimestamp(origin / 1000, tz=UTC).isoformat()
    return ""


def _summarize(etype: str, content: dict[str, Any]) -> str:
    if etype == PHASE_CHANGE_EVENT:
        return (
            f"phase {content.get('from_phase', '?')} → "
            f"{content.get('to_phase', '?')}: {content.get('reason', '')}"
        )
    if etype == TASK_EVENT:
        return (
            f"task {str(content.get('task_id', ''))[:8]} "
            f"[{content.get('status', '?')}]: "
            f"{content.get('description', '')}"
        )
    if etype == TASK_RESULT_EVENT:
        badge = "✓" if content.get("success") else "✗"
        return (
            f"{badge} task {str(content.get('task_id', ''))[:8]} "
            f"({len(content.get('artifacts') or [])} artifact(s))"
        )
    if etype == LEARNING_EVENT:
        cat = content.get("category", "?")
        text = content.get("content", "")[:80]
        conf = content.get("confidence", 0)
        return f"learning [{cat}] (conf={conf:.2f}): {text}"
    return etype or "(unknown)"
