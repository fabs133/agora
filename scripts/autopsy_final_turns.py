"""Autopsy failed task-cells in a campaign: final-turn shape + mark_complete args.

Read-only over ``runs_out/<campaign>/r*/`` (run.jsonl / tasks.jsonl / run.log).
No inference — every classification is a deterministic read of what the logs
already recorded.

For every FAILED (run, task) cell it labels the **final assistant turn** as one
of {empty, prose_no_call, malformed_call, max_iter}, from the last ``llm return``
line for that task:

  empty          final turn emitted no tool call and no text.
  prose_no_call  no tool call, but non-empty text (narrated instead of calling).
  malformed_call final turn DID emit a tool call and its result was an ERROR.
  max_iter       final turn emitted a (non-errored) call yet the task still
                 failed — the loop was cut off mid-work, not voluntarily stopped.

Separately it tallies **every** ``mark_complete`` invocation's argument shape:

  summary_ok       has a ``summary`` key (the contract).
  write_file_args  has ``path`` + ``content`` (write_file's args) and no summary
                   — the defect: mark_complete then raises KeyError('summary').
  other_malformed  neither shape.

Emits a per-model markdown summary to stdout. Redirect to save.

Usage:  python scripts/autopsy_final_turns.py --campaign runs_out/<dir>
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

FINAL_TURN_CLASSES = ("empty", "prose_no_call", "malformed_call", "max_iter")
MARK_COMPLETE_CLASSES = ("summary_ok", "write_file_args", "other_malformed")

_LLM_RETURN = re.compile(
    r"llm return: task=(?P<task>\S+) turn=(?P<turn>\d+) "
    r"tool_calls=(?P<calls>\d+) content_len=(?P<clen>\d+)"
)
_TOOL_CALL = re.compile(
    r"tool call: task=(?P<task>\S+) turn=(?P<turn>\d+) name=(?P<name>\S+) "
    r"args=(?P<args>\{.*?\}) result=(?P<result>.*)$"
)


def _base(task: str) -> str:
    """Run.log task labels are ``base:subtask``; tasks.jsonl uses ``base``."""
    return task.split(":", 1)[0]


def classify_mark_complete(args: dict) -> str:
    if "summary" in args:
        return "summary_ok"
    if "path" in args and "content" in args:
        return "write_file_args"
    return "other_malformed"


def classify_final_turn(calls: int, content_len: int, errored: bool) -> str:
    if calls > 0:
        return "malformed_call" if errored else "max_iter"
    return "prose_no_call" if content_len > 0 else "empty"


def parse_run_log(text: str) -> tuple[dict, set, list]:
    """Return (finals, errored_turns, mark_complete_classes).

    finals: base_task -> (final_turn, calls, content_len).
    errored_turns: {(base_task, turn)} where a tool call returned an ERROR.
    mark_complete_classes: one class string per mark_complete call.
    """
    finals: dict[str, tuple[int, int, int]] = {}
    errored: set[tuple[str, int]] = set()
    mark_complete: list[str] = []
    for line in text.splitlines():
        m = _LLM_RETURN.search(line)
        if m:
            base, turn = _base(m["task"]), int(m["turn"])
            prev = finals.get(base)
            if prev is None or turn > prev[0]:
                finals[base] = (turn, int(m["calls"]), int(m["clen"]))
            continue
        m = _TOOL_CALL.search(line)
        if m:
            base, turn = _base(m["task"]), int(m["turn"])
            if m["result"].startswith("ERROR"):
                errored.add((base, turn))
            if m["name"] == "mark_complete":
                try:
                    args = ast.literal_eval(m["args"])
                except (ValueError, SyntaxError):
                    args = {}
                mark_complete.append(
                    classify_mark_complete(args if isinstance(args, dict) else {})
                )
    return finals, errored, mark_complete


def _failed_task_ids(tasks_path: Path) -> list[str]:
    out = []
    for line in tasks_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        t = json.loads(line)
        if t.get("status") not in ("passed", "skipped"):
            out.append(t["task_id"])
    return out


def autopsy(campaign_dir: Path) -> dict:
    """Aggregate per-model final-turn and mark_complete tallies over a campaign."""
    finals_by_model: dict[str, Counter] = {}
    mc_by_model: dict[str, Counter] = {}
    for run_dir in sorted(p for p in campaign_dir.iterdir() if (p / "run.jsonl").is_file()):
        run = json.loads((run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()[0])
        model = run["profile"]["name"] or run["profile"]["model"]
        finals_by_model.setdefault(model, Counter())
        mc_by_model.setdefault(model, Counter())
        log = (run_dir / "run.log")
        finals, errored, mc = parse_run_log(log.read_text(encoding="utf-8")) if log.is_file() else ({}, set(), [])
        for cls in mc:
            mc_by_model[model][cls] += 1
        for task_id in _failed_task_ids(run_dir / "tasks.jsonl"):
            final = finals.get(task_id)
            if final is None:
                finals_by_model[model]["empty"] += 1  # no turns logged (rare)
                continue
            turn, calls, clen = final
            cls = classify_final_turn(calls, clen, (task_id, turn) in errored)
            finals_by_model[model][cls] += 1
    return {"finals": finals_by_model, "mark_complete": mc_by_model}


def _table(title: str, per_model: dict, classes: tuple, count_label: str) -> list[str]:
    lines = [f"## {title}", "", f"| model | {count_label} | " + " | ".join(classes) + " |",
             "|---|" + "---|" * (len(classes) + 1)]
    total = Counter()
    for model in sorted(per_model):
        c = per_model[model]
        n = sum(c.values())
        total.update(c)
        lines.append(f"| {model} | {n} | " + " | ".join(str(c.get(k, 0)) for k in classes) + " |")
    lines.append(f"| **TOTAL** | **{sum(total.values())}** | "
                 + " | ".join(f"**{total.get(k, 0)}**" for k in classes) + " |")
    return lines + [""]


def render(campaign_dir: Path, data: dict) -> str:
    lines = [f"# Final-turn & mark_complete autopsy — `{campaign_dir.name}`", "",
             "Read-only classification of failed task-cells. No inference; see the "
             "script docstring for the exact rules.", ""]
    lines += _table("Final assistant turn on failed cells", data["finals"],
                    FINAL_TURN_CLASSES, "failed cells")
    lines += _table("mark_complete argument patterns (all invocations)",
                    data["mark_complete"], MARK_COMPLETE_CLASSES, "calls")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # The report contains non-ASCII (em dash); Windows redirected stdout defaults
    # to cp1252 and would mojibake it. Force UTF-8 so a `> file` redirect is clean.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # already utf-8, or captured (pytest)
        pass
    parser = argparse.ArgumentParser(description="Autopsy a campaign's failed cells.")
    parser.add_argument("--campaign", required=True, help="campaign output dir")
    args = parser.parse_args(argv)
    campaign_dir = Path(args.campaign)
    print(render(campaign_dir, autopsy(campaign_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
