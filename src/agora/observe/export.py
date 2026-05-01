"""Standalone HTML report generation from the Matrix event graph."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from agora.core.types import TaskStatus
from agora.observe.kanban import KanbanBoard, build_kanban
from agora.observe.timeline import TimelineEntry, build_timeline

_EMBED_CSS = """
body { font-family: system-ui, -apple-system, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
h1, h2, h3 { color: #0a0a0a; }
.meta { color: #555; font-size: 0.9rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; text-align: left; font-size: 0.9rem; }
th { background: #f6f6f6; }
.badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.8rem; color: white; }
.b-done    { background: #2a7a4b; }
.b-failed  { background: #a23131; }
.b-running { background: #2e6a8a; }
.b-pending { background: #777; }
.b-review  { background: #a77; }
.b-assigned{ background: #555; }
.bar { display: inline-block; width: 12rem; height: 0.5rem; background: #eee; border-radius: 3px; overflow: hidden; vertical-align: middle; }
.bar > span { display: block; height: 100%; background: #2a7a4b; }
.timeline { border-left: 3px solid #ccc; margin: 1rem 0; padding: 0 0 0 1rem; }
.timeline .entry { margin-bottom: 0.6rem; }
.timeline .ts { color: #888; font-family: ui-monospace, monospace; font-size: 0.8rem; }
code { background: #f3f3f3; padding: 0.05rem 0.3rem; border-radius: 3px; font-family: ui-monospace, monospace; }
""".strip()


@dataclass(frozen=True)
class ReportContext:
    project_name: str
    project_id: str
    phase: str
    started_at: str
    ended_at: str
    total_tokens: dict[str, int]
    duration_seconds: float
    agents: list[str]


def render_report(
    context: ReportContext,
    events: Iterable[tuple[str, dict[str, Any]]],
    learnings_by_agent: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """Produce a standalone HTML document summarising the project."""
    events_list = list(events)
    kanban = build_kanban(event for _room, event in events_list)
    timeline = build_timeline(events_list)

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>Agora report — {escape(context.project_name)}</title>",
        f"<style>{_EMBED_CSS}</style>",
        "</head><body>",
        f"<h1>Agora report — {escape(context.project_name)}</h1>",
        _render_meta(context),
        _render_kanban(kanban),
        _render_timeline(timeline),
        _render_learnings(learnings_by_agent or {}),
        _render_footer(),
        "</body></html>",
    ]
    return "\n".join(parts)


def write_report(
    path: str | Path,
    context: ReportContext,
    events: Iterable[tuple[str, dict[str, Any]]],
    learnings_by_agent: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_report(context, events, learnings_by_agent), encoding="utf-8"
    )
    return str(out)


# ---------------------------------------------------------------- sections


def _render_meta(ctx: ReportContext) -> str:
    lines = [
        "<section class='meta'>",
        f"<p><strong>Project ID:</strong> <code>{escape(ctx.project_id)}</code></p>",
        f"<p><strong>Final phase:</strong> <code>{escape(ctx.phase)}</code></p>",
        f"<p><strong>Agents:</strong> {escape(', '.join(ctx.agents) or '(none)')}</p>",
        f"<p><strong>Started:</strong> {escape(ctx.started_at)}</p>",
        f"<p><strong>Finished:</strong> {escape(ctx.ended_at)}</p>",
        f"<p><strong>Duration:</strong> {ctx.duration_seconds:.1f}s</p>",
        (
            f"<p><strong>Tokens:</strong> in={ctx.total_tokens.get('input_tokens', 0)}, "
            f"out={ctx.total_tokens.get('output_tokens', 0)}</p>"
        ),
        "</section>",
    ]
    return "\n".join(lines)


def _render_kanban(kanban: KanbanBoard) -> str:
    parts = ["<h2>Kanban (final state)</h2>", "<table><thead><tr>"]
    for status in TaskStatus:
        parts.append(f"<th>{escape(status.value)}</th>")
    parts.append("</tr></thead><tbody><tr>")
    max_rows = max((len(cards) for cards in kanban.columns.values()), default=0)
    for status in TaskStatus:
        cards = kanban.columns.get(status, [])
        cells = []
        for card in cards:
            cells.append(
                f"<div><span class='badge b-{status.value}'>{escape(status.value)}</span> "
                f"<code>{escape(card.id[:8])}</code> "
                f"{escape(card.description or '')}</div>"
            )
        # Pad so each column reaches max_rows height visually.
        while len(cells) < max_rows:
            cells.append("&nbsp;")
        parts.append("<td>" + "".join(cells) + "</td>")
    parts.append("</tr></tbody></table>")
    return "\n".join(parts)


def _render_timeline(entries: list[TimelineEntry]) -> str:
    parts = ["<h2>Project journey</h2>", "<div class='timeline'>"]
    for entry in entries:
        parts.append(
            "<div class='entry'>"
            f"<span class='ts'>{escape(entry.timestamp or '?')}</span> "
            f"<span class='badge'>[{escape(entry.category)}]</span> "
            f"{escape(entry.summary)}"
            "</div>"
        )
    parts.append("</div>")
    return "\n".join(parts)


def _render_learnings(learnings_by_agent: dict[str, list[dict[str, Any]]]) -> str:
    if not learnings_by_agent:
        return "<h2>Learnings</h2><p><em>None recorded.</em></p>"
    parts = ["<h2>Learnings</h2>"]
    for agent, rows in sorted(learnings_by_agent.items()):
        parts.append(f"<h3>{escape(agent)}</h3>")
        parts.append("<table><thead><tr>")
        parts.extend(f"<th>{h}</th>" for h in ("category", "content", "confidence", "uses"))
        parts.append("</tr></thead><tbody>")
        for row in sorted(rows, key=lambda r: r.get("confidence", 0), reverse=True):
            conf = float(row.get("confidence", 0))
            parts.append(
                "<tr>"
                f"<td>{escape(str(row.get('category', '?')))}</td>"
                f"<td>{escape(str(row.get('content', '')))}</td>"
                f"<td><div class='bar'><span style='width:{int(conf*100)}%'></span></div>"
                f" {int(conf*100)}%</td>"
                f"<td>{int(row.get('reinforcement_count', 0))}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_footer() -> str:
    generated = datetime.now(timezone.utc).isoformat()
    return (
        f"<hr><p class='meta'>Generated by Agora at {escape(generated)}. "
        "All data derived from the Matrix event graph.</p>"
    )
