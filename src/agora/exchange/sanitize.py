"""Scrub machine-private strings from a submission (capability-program L2-1).

The registry.yaml lesson at community scale: a contributed record must carry NO
username, hostname, or home path. This redacts the obvious private tokens (the
current user, host, and home directory — plus any caller-supplied extras) from
every string field of the records + attestation, and returns a scrub REPORT so
the contributor sees exactly what was removed before anything leaves the machine.
"""

from __future__ import annotations

import getpass
import socket
from pathlib import Path
from typing import Any


def private_tokens(extra: dict[str, str] | None = None) -> dict[str, str]:
    """``{private string -> replacement}``, ordered longest-first so a home path
    (which contains the username) is redacted before the bare username."""
    toks: dict[str, str] = {}
    try:
        toks[str(Path.home())] = "[HOME]"
    except Exception:  # noqa: BLE001
        pass
    try:
        user = getpass.getuser()
        if user:
            toks[user] = "[USER]"
    except Exception:  # noqa: BLE001
        pass
    try:
        host = socket.gethostname()
        if host:
            toks[host] = "[HOST]"
    except Exception:  # noqa: BLE001
        pass
    if extra:
        toks.update(extra)
    return dict(sorted(toks.items(), key=lambda kv: -len(kv[0])))


def _scrub(text: str, tokens: dict[str, str], report: dict[str, int]) -> str:
    for tok, repl in tokens.items():
        if tok and tok in text:
            report[tok] = report.get(tok, 0) + text.count(tok)
            text = text.replace(tok, repl)
    return text


def _walk(obj: Any, tokens: dict[str, str], report: dict[str, int]) -> Any:
    if isinstance(obj, str):
        return _scrub(obj, tokens, report)
    if isinstance(obj, dict):
        return {k: _walk(v, tokens, report) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, tokens, report) for v in obj]
    return obj


def sanitize_submission(
    run_records: list[Any],
    task_records: list[Any],
    attestation: Any,
    *,
    tokens: dict[str, str] | None = None,
) -> tuple[list[Any], list[Any], Any, dict[str, int]]:
    """Return ``(runs, tasks, attestation, report)`` with private tokens redacted.

    Only string fields are touched and no vector-affecting field (tool-call
    counts, statuses) is a private token, so a sanitized submission still
    re-derives identically — the validator runs on the sanitized copy.
    """
    from agora.exchange.schema import Attestation
    from agora.observe.jsonl import RunRecord, TaskRecord

    tokens = tokens if tokens is not None else private_tokens()
    report: dict[str, int] = {}
    runs = [RunRecord.model_validate(_walk(r.model_dump(), tokens, report)) for r in run_records]
    tasks = [TaskRecord.model_validate(_walk(t.model_dump(), tokens, report)) for t in task_records]
    att = Attestation.model_validate(_walk(attestation.model_dump(), tokens, report))
    return runs, tasks, att, report


def scrub_report_lines(report: dict[str, int]) -> list[str]:
    if not report:
        return ["no machine-private strings found"]
    return [f"redacted {tok!r} ({n}x)" for tok, n in sorted(report.items())]


__all__ = ["private_tokens", "sanitize_submission", "scrub_report_lines"]
