"""P3: locate the first-divergence turn across a task's non-deterministic modes.

Read-only over a campaign's run dirs. Groups a model's runs of one task by their
outcome mode (status, tool_calls_total), extracts each mode's per-turn generation
signature (turn, tool_calls, content_len) from run.log, and reports the first turn
index at which the modes diverge — the determinism-probe P3 question for qwen3's
v2 small_chain four modes.

(The axis-1 autopsy tooling — scripts/autopsy_final_turns.py — lives on the
unmerged chore/axis-1-postcampaign branch; this standalone tool is the same
read-only run.log style.)

Usage:  python scripts/p3_qwen3_divergence.py <campaign_dir> <model_substr> <task_id>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_LLM_RETURN = re.compile(
    r"llm return: task=(?P<task>\S+) turn=(?P<turn>\d+) "
    r"tool_calls=(?P<calls>\d+) content_len=(?P<clen>\d+)"
)


def _base(task: str) -> str:
    return task.split(":", 1)[0]


def turn_signatures(log_text: str, task_id: str) -> list[tuple[int, int, int]]:
    """Per-turn (turn, tool_calls, content_len) for a task, from its llm-return
    lines — the observable per-turn generation signature."""
    sigs: list[tuple[int, int, int]] = []
    for line in log_text.splitlines():
        m = _LLM_RETURN.search(line)
        if m and _base(m["task"]) == task_id:
            sigs.append((int(m["turn"]), int(m["calls"]), int(m["clen"])))
    return sigs


def first_divergence(sequences: list[list[tuple[int, int, int]]]) -> int | None:
    """First turn index at which the signature sequences are not all equal
    (None if every sequence is identical)."""
    if len(sequences) < 2:
        return None
    for i in range(min(len(s) for s in sequences)):
        if len({tuple(s[i]) for s in sequences}) > 1:
            return i
    if len({len(s) for s in sequences}) > 1:
        return min(len(s) for s in sequences)  # diverge by length (a mode stops)
    return None


def analyse(campaign_dir: Path, model_substr: str, task_id: str) -> dict:
    """Group runs by mode, one representative each, with turn signatures."""
    modes: dict[tuple[str, int], dict] = {}
    for run_dir in sorted(p for p in campaign_dir.iterdir() if (p / "run.jsonl").is_file()):
        run = json.loads((run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()[0])
        model = run["profile"]["name"] or run["profile"]["model"]
        if model_substr not in model:
            continue
        task = next(
            (t for t in (json.loads(x) for x in
                         (run_dir / "tasks.jsonl").read_text(encoding="utf-8").splitlines() if x.strip())
             if t["task_id"] == task_id),
            None,
        )
        if task is None:
            continue
        key = (task["status"], task["tool_calls_total"])
        m = modes.setdefault(key, {"runs": [], "sig": None})
        m["runs"].append(run_dir.name)
        if m["sig"] is None:
            m["sig"] = turn_signatures((run_dir / "run.log").read_text(encoding="utf-8"), task_id)
    return modes


def build_report(campaign_dir: Path, model_substr: str, task_id: str) -> str:
    modes = analyse(campaign_dir, model_substr, task_id)
    ordered = sorted(modes.items(), key=lambda kv: (-len(kv[1]["runs"]), kv[0]))
    lines = [
        f"# P3 first-divergence — `{campaign_dir.name}` {model_substr} `{task_id}`",
        "",
        "Per-turn signature = (turn, tool_calls, content_len). One representative "
        "run per mode.",
        "",
        "| mode (status, calls) | n | runs | per-turn signatures |",
        "|---|---|---|---|",
    ]
    for (status, calls), m in ordered:
        sig = " ".join(f"t{t}:c{c}/len{cl}" for t, c, cl in m["sig"])
        lines.append(f"| {status}/{calls} | {len(m['runs'])} | {','.join(m['runs'])} | {sig} |")
    seqs = [m["sig"] for _, m in ordered]
    fd = first_divergence(seqs)
    lines += ["", f"**First divergence across all {len(ordered)} modes: turn index "
              f"{fd if fd is not None else 'none (identical)'}** "
              f"(turn {fd + 1 if fd is not None else '—'}, 1-based)."]
    # Pairwise among same-turn-1 modes.
    if len(ordered) >= 2:
        lines.append("")
        lines.append("| mode A | mode B | first-divergence turn (0-based) |")
        lines.append("|---|---|---|")
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                fdij = first_divergence([a[1]["sig"], b[1]["sig"]])
                lines.append(f"| {a[0][0]}/{a[0][1]} | {b[0][0]}/{b[0][1]} | "
                             f"{fdij if fdij is not None else 'identical'} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="P3 mode first-divergence.")
    parser.add_argument("campaign")
    parser.add_argument("model_substr")
    parser.add_argument("task_id")
    args = parser.parse_args(argv)
    print(build_report(Path(args.campaign), args.model_substr, args.task_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
