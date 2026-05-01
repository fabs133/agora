"""Render domain events as HTML-formatted Matrix messages.

Every formatter returns :class:`FormattedMessage` — a ``content`` dict ready to
hand to ``matrix_client.send_event(room_id, "m.room.message", content)``. We
produce both a plain ``body`` (for clients that don't render HTML) and a
``formatted_body`` with ``org.matrix.custom.html`` for Element.

Golden-tested: these functions are pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from typing import Any

from agora.core.learning import Learning
from agora.core.project import PhaseChange
from agora.core.types import ProjectPhase
from agora.observe.commands import HELP_TEXT


@dataclass(frozen=True)
class FormattedMessage:
    body: str
    formatted_body: str
    msgtype: str = "m.notice"  # notice = bot, so clients dim them

    def to_content(self) -> dict[str, Any]:
        return {
            "msgtype": self.msgtype,
            "body": self.body,
            "format": "org.matrix.custom.html",
            "formatted_body": self.formatted_body,
        }


# ---------------------------------------------------------------- phase change


_PHASE_EMOJI: dict[ProjectPhase, str] = {
    ProjectPhase.INIT: "🌱",
    ProjectPhase.ANALYSIS: "📝",
    ProjectPhase.ARCHITECTURE: "🏗️",
    ProjectPhase.IMPLEMENTATION: "🔧",
    ProjectPhase.TESTING: "🧪",
    ProjectPhase.REVIEW: "👀",
    ProjectPhase.DONE: "✅",
    ProjectPhase.FAILED: "💥",
}


def format_phase_change(change: PhaseChange) -> FormattedMessage:
    emoji = _PHASE_EMOJI.get(change.to_phase, "•")
    headline = f"{emoji} phase: {change.from_phase.value} → {change.to_phase.value}"
    body = f"{headline}\n{change.reason}" if change.reason else headline
    html = (
        f"<h4>{escape(headline)}</h4>"
        + (f"<p><em>{escape(change.reason)}</em></p>" if change.reason else "")
    )
    return FormattedMessage(body=body, formatted_body=html)


# ---------------------------------------------------------------- task events


def format_task_started(task_content: dict[str, Any]) -> FormattedMessage:
    tid = str(task_content.get("task_id", ""))[:8]
    desc = task_content.get("description", "")
    agent = task_content.get("agent_id") or "?"
    body = f"▶ task {tid} ({agent}): {desc}"
    html = (
        f"<p>▶ <strong>task {escape(tid)}</strong> "
        f"(<code>{escape(str(agent))}</code>): {escape(desc)}</p>"
    )
    return FormattedMessage(body=body, formatted_body=html)


def format_task_completed(result_content: dict[str, Any]) -> FormattedMessage:
    tid = str(result_content.get("task_id", ""))[:8]
    success = bool(result_content.get("success"))
    badge = "✓" if success else "✗"
    artifacts = result_content.get("artifacts") or []
    pcr = result_content.get("postcondition_results") or []
    body_lines = [f"{badge} task {tid}"]
    if artifacts:
        body_lines.append(f"  artifacts: {', '.join(artifacts)}")
    for rec in pcr:
        name = rec.get("name") if isinstance(rec, dict) else rec[0]
        passed = rec.get("passed") if isinstance(rec, dict) else rec[1]
        body_lines.append(f"    - {'✓' if passed else '✗'} {name}")

    html_bits = [
        f"<h4>{badge} task <code>{escape(tid)}</code></h4>",
    ]
    if artifacts:
        html_bits.append(
            "<p>artifacts: "
            + ", ".join(f"<code>{escape(str(a))}</code>" for a in artifacts)
            + "</p>"
        )
    if pcr:
        lis: list[str] = []
        for rec in pcr:
            if isinstance(rec, dict):
                name, passed, reason = rec.get("name", ""), bool(rec.get("passed")), rec.get("reason", "")
            else:
                name, passed, reason = rec[0], bool(rec[1]), rec[2] if len(rec) > 2 else ""
            mark = "✓" if passed else "✗"
            suffix = f" — <em>{escape(str(reason))}</em>" if reason and not passed else ""
            lis.append(f"<li>{mark} <code>{escape(str(name))}</code>{suffix}</li>")
        html_bits.append("<ul>" + "".join(lis) + "</ul>")

    return FormattedMessage(body="\n".join(body_lines), formatted_body="".join(html_bits))


# ---------------------------------------------------------------- learning


def format_learning(learning: Learning) -> FormattedMessage:
    pct = int(round(learning.confidence * 100))
    bar_full = max(0, min(20, pct // 5))
    bar = "█" * bar_full + "░" * (20 - bar_full)
    body = f"💡 [{learning.category.value}] ({pct:>3}%) {learning.content}"
    html = (
        f"<p>💡 <strong>{escape(learning.category.value)}</strong> "
        f"<code>{bar}</code> {pct}%: {escape(learning.content)}</p>"
    )
    return FormattedMessage(body=body, formatted_body=html)


# ---------------------------------------------------------------- review summary


@dataclass(frozen=True)
class ArtifactSnapshot:
    """What actually exists on disk at review time.

    ReviewCoordinator fills this in so the reviewer sees concrete files +
    recent commits + any failing gate before the vote, instead of voting on a
    black box.
    """

    #: ``[(relative_path, byte_count), ...]`` sorted by path.
    files: list[tuple[str, int]]
    #: Short ``git log --oneline`` entries, most recent first.
    recent_commits: list[str]
    #: ``[(task_id, predicate_name, reason), ...]`` for every failed postcondition.
    postcondition_failures: list[tuple[str, str, str]]
    #: ``{task_id: {emoji_key: count}}`` aggregated across all reactions the
    #: reviewer left on that task's cards during the run. Empty dict when no
    #: reactions were captured.
    reaction_counts: dict[str, dict[str, int]] = field(default_factory=dict)


def format_review_summary(
    project_name: str,
    phase: ProjectPhase,
    task_results_summary: list[Any],
    artifact: ArtifactSnapshot | None = None,
) -> FormattedMessage:
    """Render a review summary.

    Accepts either dicts (MCP path) or :class:`TaskResult` dataclass instances
    (orchestrator path); both carry ``success`` / ``task_id`` fields.

    When ``artifact`` is provided, the review summary also shows the file
    tree, recent commits, and any failed postconditions — so the reviewer
    votes on concrete state rather than a name alone.
    """

    def _get(r: Any, attr: str, default: Any = "") -> Any:
        if isinstance(r, dict):
            return r.get(attr, default)
        return getattr(r, attr, default)

    passed = sum(1 for r in task_results_summary if _get(r, "success"))
    failed = len(task_results_summary) - passed
    body_lines = [
        f"👀 REVIEW — project '{project_name}' (phase {phase.value})",
        f"  {passed} passed, {failed} failed across {len(task_results_summary)} task(s)",
    ]
    if artifact is not None:
        body_lines.append("")
        body_lines.append(f"📂 {len(artifact.files)} file(s) on disk:")
        for path, size in artifact.files[:20]:
            body_lines.append(f"   {path}  ({size} bytes)")
        if len(artifact.files) > 20:
            body_lines.append(f"   … +{len(artifact.files) - 20} more")
        if artifact.recent_commits:
            body_lines.append("")
            body_lines.append("🔖 recent commits:")
            for line in artifact.recent_commits[:10]:
                body_lines.append(f"   {line}")
        if artifact.postcondition_failures:
            body_lines.append("")
            body_lines.append(
                f"⚠ {len(artifact.postcondition_failures)} failed postcondition(s):"
            )
            for task_id, pred, reason in artifact.postcondition_failures[:10]:
                snippet = reason.strip().splitlines()[0][:120] if reason else ""
                body_lines.append(f"   [{task_id}] {pred}: {snippet}")
        if artifact.reaction_counts:
            body_lines.append("")
            body_lines.append("👉 reviewer signal (your own reactions):")
            for task_id, counts in artifact.reaction_counts.items():
                parts = " ".join(f"{key} ×{n}" for key, n in counts.items())
                body_lines.append(f"   {task_id}: {parts}")
    body_lines.append("")
    body_lines.append(
        "Vote via the poll below, or type one of:\n"
        "  /agora review approve            — ship it\n"
        "  /agora review reject             — loop back to implementation (alias)\n"
        "  /agora review reject_analysis    — loop back to analysis\n"
        "  /agora review reject_architecture — loop back to architecture\n"
        "  /agora review reject_testing     — loop back to testing"
    )

    rows = "".join(
        f"<tr><td>{'✓' if _get(r, 'success') else '✗'}</td>"
        f"<td><code>{escape(str(_get(r, 'task_id', ''))[:8])}</code></td>"
        f"<td>{escape(str(_get(r, 'description', '')))}</td></tr>"
        for r in task_results_summary
    )
    html_parts: list[str] = [
        f"<h3>👀 Review for <em>{escape(project_name)}</em></h3>",
        (
            f"<p><strong>{passed}</strong> passed / <strong>{failed}</strong> failed "
            f"(phase <code>{phase.value}</code>).</p>"
        ),
        f"<table>{rows}</table>",
    ]
    if artifact is not None:
        if artifact.files:
            file_rows = "".join(
                f"<tr><td><code>{escape(p)}</code></td>"
                f"<td align='right'>{n:,} B</td></tr>"
                for p, n in artifact.files[:20]
            )
            tail = (
                f"<tr><td colspan='2'><em>… +{len(artifact.files) - 20} more</em></td></tr>"
                if len(artifact.files) > 20 else ""
            )
            html_parts.append(f"<h4>📂 Files on disk</h4><table>{file_rows}{tail}</table>")
        if artifact.recent_commits:
            commit_items = "".join(
                f"<li><code>{escape(c)}</code></li>"
                for c in artifact.recent_commits[:10]
            )
            html_parts.append(f"<h4>🔖 Recent commits</h4><ul>{commit_items}</ul>")
        if artifact.postcondition_failures:
            fail_items = "".join(
                (
                    f"<li><code>{escape(task_id)}</code> · <strong>{escape(pred)}</strong>: "
                    f"{escape(reason.strip().splitlines()[0][:200] if reason else '')}</li>"
                )
                for task_id, pred, reason in artifact.postcondition_failures[:10]
            )
            html_parts.append(
                "<h4>⚠ Failed postconditions</h4>"
                f"<ul>{fail_items}</ul>"
            )
        if artifact.reaction_counts:
            rows = "".join(
                (
                    f"<tr><td><code>{escape(task_id)}</code></td>"
                    f"<td>"
                    + " ".join(
                        f"{escape(key)}&nbsp;<strong>×{n}</strong>"
                        for key, n in counts.items()
                    )
                    + "</td></tr>"
                )
                for task_id, counts in artifact.reaction_counts.items()
            )
            html_parts.append(
                "<h4>👉 Reviewer signal</h4>"
                f"<table>{rows}</table>"
            )
    html_parts.append(
        "<p>Cast your vote with the poll below, or reply with one of:</p>"
        "<ul>"
        "<li><code>/agora review approve</code> — ship it</li>"
        "<li><code>/agora review reject</code> — loop back to implementation "
        "<em>(alias for reject_implementation)</em></li>"
        "<li><code>/agora review reject_analysis</code> · "
        "<code>reject_architecture</code> · "
        "<code>reject_testing</code> — loop back to those phases</li>"
        "</ul>"
    )
    return FormattedMessage(body="\n".join(body_lines), formatted_body="".join(html_parts))


# ---------------------------------------------------------------- help / misc


def format_help() -> FormattedMessage:
    html = "<pre>" + escape(HELP_TEXT) + "</pre>"
    return FormattedMessage(body=HELP_TEXT, formatted_body=html)


def format_command_reference() -> FormattedMessage:
    """A compact, collapsed cheat sheet for the observer commands.

    Posted once per project at observer attach. Element users can expand the
    ``<details>`` block to see every verb without typing ``/agora help``.
    """
    body_lines = [
        "📋 Observer quick reference (expand in Element)",
        "  Per-task:",
        "    ✅ react — positive signal",
        "    🔁 react — reviewer wants this task retried on next loopback",
        "    💬 reply to a task card — adds that text as feedback for the task",
        "  Project-level:",
        "    /agora pause | /agora resume       — halt / continue",
        "    /agora abort [reason]              — cancel the project",
        "    /agora note <text>                 — attach a note for agents",
        "    /agora redirect <agent> <text>     — rewrite one agent's instructions",
        "    /agora comment <task_id> <text>    — same as replying to a card",
        "    /agora review <answer_id>          — vote fallback if the poll is missing",
    ]
    html = (
        "<details>"
        "<summary>📋 <strong>Observer quick reference</strong> (click to expand)</summary>"
        "<h4>Per-task (just tap)</h4>"
        "<ul>"
        "<li>✅ react to a task card — positive signal</li>"
        "<li>🔁 react to a task card — reviewer wants this task retried on next loopback</li>"
        "<li>💬 reply to a task card — adds that text as feedback for the task</li>"
        "</ul>"
        "<h4>Project-level</h4>"
        "<ul>"
        "<li><code>/agora pause</code> · <code>/agora resume</code></li>"
        "<li><code>/agora abort [reason]</code></li>"
        "<li><code>/agora note &lt;text&gt;</code></li>"
        "<li><code>/agora redirect &lt;agent&gt; &lt;text&gt;</code></li>"
        "<li><code>/agora comment &lt;task_id&gt; &lt;text&gt;</code> — same as replying to a card</li>"
        "<li><code>/agora review &lt;answer_id&gt;</code> — vote fallback if the poll is missing</li>"
        "</ul>"
        "</details>"
    )
    return FormattedMessage(body="\n".join(body_lines), formatted_body=html)


def format_note(sender: str, text: str) -> FormattedMessage:
    body = f"📌 note from {sender}: {text}"
    html = (
        f"<p>📌 <strong>note</strong> from <code>{escape(sender)}</code>: "
        f"{escape(text)}</p>"
    )
    return FormattedMessage(body=body, formatted_body=html)


def format_ack(text: str) -> FormattedMessage:
    body = f"✓ {text}"
    html = f"<p>✓ {escape(text)}</p>"
    return FormattedMessage(body=body, formatted_body=html)


def format_error(text: str) -> FormattedMessage:
    body = f"⚠ {text}"
    html = f"<p>⚠ <em>{escape(text)}</em></p>"
    return FormattedMessage(body=body, formatted_body=html)


# ---------------------------------------------------------------- write events


def format_write_event(
    *,
    task_id: str,
    path: str,
    operation: str,
    size_bytes: int,
    hook_summary: list[tuple[str, bool]],
    preview: str | None = None,
) -> FormattedMessage:
    """Render a compact card for a file-write event.

    ``operation`` is e.g. ``"write"``, ``"edit:replace"``, ``"edit:insert_before"``,
    ``"edit:append"``, ``"fetch:save_as"``. ``hook_summary`` is a list of
    ``(tool_name, success)`` tuples from the auto-hook chain.

    ``preview`` is an optional short (< ~2 KB) snippet of what was written;
    it's rendered in a collapsed ``<details>`` block so the room stays legible
    on scroll but the reviewer can expand and see the actual bytes.
    """
    icons = {True: "✓", False: "✗"}
    chain_plain = " → ".join(f"{icons[ok]} {name}" for name, ok in hook_summary) or "(no hooks)"
    chain_html = " → ".join(
        f"{icons[ok]} <code>{escape(name)}</code>" for name, ok in hook_summary
    ) or "<em>(no hooks)</em>"

    reaction_hint = (
        "↳ react: ✅ looks good · 🔁 retry this task · 💬 reply to add feedback"
    )

    body = (
        f"✎ {path} — {operation} ({size_bytes:,} B) [{task_id}]\n"
        f"  {chain_plain}\n"
        f"  {reaction_hint}"
    )
    html_parts = [
        f"<p>✎ <code>{escape(path)}</code> — "
        f"<em>{escape(operation)}</em> "
        f"(<strong>{size_bytes:,}</strong> B) "
        f"[<code>{escape(task_id)}</code>]</p>",
        f"<p>{chain_html}</p>",
    ]
    if preview:
        snippet = preview[:2048]
        html_parts.append(
            "<details><summary>preview</summary>"
            f"<pre><code>{escape(snippet)}</code></pre>"
            "</details>"
        )
    html_parts.append(
        f"<p><small>↳ react: ✅ looks good · 🔁 retry this task · "
        f"💬 reply to this message with feedback</small></p>"
    )
    return FormattedMessage(body=body, formatted_body="".join(html_parts))
