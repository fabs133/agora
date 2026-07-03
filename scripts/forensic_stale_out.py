"""Forensic classifier: separate live task outcomes from stale-workspace artifacts.

Read-only over a campaign's run dirs. The probe never reset ``out/`` between runs,
so a write_file overwrite-guard could block the model's write and the equality
postcondition would then compare against a STALE file (axis-1 v3.0 gemma finding).
For every equality task-cell this joins the run.log write_file outcome with the
byte-exact predicate result and classifies it:

  live_pass          wrote successfully AND the equality predicate passed.
  stale_backed_pass  guard-blocked or never attempted, yet the predicate passed
                     (the on-disk stale file happened to be correct).
  guard_artifact_fail guard-blocked AND the predicate failed (the model's write
                     was suppressed; failure attributes to the stale file, not
                     the model).
  genuine_fail       wrote (or never attempted) AND the predicate failed.

Emits per-model × per-campaign markdown tables.

Usage:  python scripts/forensic_stale_out.py <campaign_dir> [<campaign_dir> ...]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

CLASSES = ("live_pass", "stale_backed_pass", "guard_artifact_fail", "genuine_fail")

# run.log truncates tool-call args to 120 chars (str(arguments)[:120]), so a
# long write_file `content` is cut mid-dict — the closing brace may be absent.
# Capture args up to the literal " result=" delimiter regardless, so the result
# (and thus the guard-block classification) is never lost to truncation.
_WRITE_CALL = re.compile(
    r"tool call: task=(?P<task>\S+) turn=\d+ name=write_file "
    r"args=(?P<args>.*?) result=(?P<result>.*)$"
)
_CONTENT_PREFIX = re.compile(r"'content':\s*'(?P<c>.*?)(?:',|$)")


def _extract_content(args_str: str) -> str | None:
    """Best-effort content value from a (possibly truncation-mangled) args repr."""
    try:
        args = ast.literal_eval(args_str)
        if isinstance(args, dict) and "content" in args:
            return args["content"]
    except (ValueError, SyntaxError):
        pass
    m = _CONTENT_PREFIX.search(args_str)  # truncated dict — grab the visible prefix
    if m:
        try:
            return m["c"].encode("utf-8", "replace").decode("unicode_escape", "replace")
        except (UnicodeDecodeError, ValueError):
            return m["c"]
    return None


def _base(task: str) -> str:
    return task.split(":", 1)[0]


def classify_write_result(result_strings: list[str]) -> str | None:
    """Reduce a task's write_file result strings to one outcome (None = no attempt)."""
    if not result_strings:
        return None
    if any(r.startswith("wrote ") for r in result_strings):
        return "success"
    if any("already exists with" in r for r in result_strings):
        return "guard_blocked"
    return "other_error"


def classify_cell(write_result: str | None, predicate_passed: bool) -> str:
    """The 4-way stale-vs-live classification for one equality task-cell."""
    if predicate_passed:
        return "live_pass" if write_result == "success" else "stale_backed_pass"
    if write_result == "guard_blocked":
        return "guard_artifact_fail"
    return "genuine_fail"  # wrote-but-wrong, never-attempted, or other write error


def parse_writes_for_task(log_text: str, task_id: str) -> tuple[list[str], str | None]:
    """Return (result_strings, first_attempted_content) for a task's write_file calls."""
    results: list[str] = []
    content: str | None = None
    for line in log_text.splitlines():
        m = _WRITE_CALL.search(line)
        if not m or _base(m["task"]) != task_id:
            continue
        results.append(m["result"])
        if content is None:
            content = _extract_content(m["args"])
    return results, content


def _equality_predicate(task: dict) -> tuple[str, bool] | None:
    """The byte-exact (``*_eq_*``) postcondition (name, passed), if present."""
    for p in task.get("postconditions", []):
        if "_eq_" in p["name"]:
            return p["name"], bool(p["passed"])
    return None


def classify_campaign(campaign_dir: Path) -> tuple[dict, list]:
    """Per (model, class) counts + captured exhibits for one campaign dir."""
    counts: dict[str, Counter] = defaultdict(Counter)
    exhibits: list[dict] = []
    for run_dir in sorted(p for p in campaign_dir.iterdir() if (p / "run.jsonl").is_file()):
        run = json.loads((run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()[0])
        model = run["profile"]["name"] or run["profile"]["model"]
        log = (run_dir / "run.log").read_text(encoding="utf-8") if (run_dir / "run.log").is_file() else ""
        for line in (run_dir / "tasks.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            task = json.loads(line)
            eq = _equality_predicate(task)
            if eq is None:
                continue
            results, content = parse_writes_for_task(log, task["task_id"])
            wres = classify_write_result(results)
            cls = classify_cell(wres, eq[1])
            counts[model][cls] += 1
            exhibits.append({
                "campaign": campaign_dir.name, "run": run_dir.name, "model": model,
                "task": task["task_id"], "write_result": wres, "class": cls,
                "content": content,
            })
    return counts, exhibits


def _table(campaign: str, counts: dict) -> list[str]:
    lines = [f"### `{campaign}`", "",
             "| model | " + " | ".join(CLASSES) + " |",
             "|---|" + "---|" * len(CLASSES)]
    total = Counter()
    for model in sorted(counts):
        c = counts[model]
        total.update(c)
        lines.append(f"| {model} | " + " | ".join(str(c.get(k, 0)) for k in CLASSES) + " |")
    lines.append("| **TOTAL** | " + " | ".join(f"**{total.get(k, 0)}**" for k in CLASSES) + " |")
    return lines + [""]


def _appendix(all_exhibits: list) -> list[str]:
    """The two verbatim exhibits: the S4 diagnosis (gemma loop_depth attempted
    concat) and the copy-safety exhibit (marker-prefixed small_chain content)."""
    lines = ["## Appendix — verbatim attempted-write content (exhibits)", ""]

    def _find(task: str, want_marker: bool) -> dict | None:
        for e in all_exhibits:
            c = e["content"]
            if e["task"] == task and "gemma" in e["model"] and c and (("[read_file#" in c) == want_marker):
                return e
        return None

    ld = _find("loop_depth", want_marker=False)
    if ld:
        lines += [
            "**S4 diagnosis — gemma `loop_depth` attempted content** "
            f"(`{ld['campaign']}/{ld['run']}`, write {ld['write_result']}, "
            f"classified {ld['class']}). The newline-join is byte-correct — the "
            "failure is the guard block against a stale file, not the model:",
            "", "```", repr(ld["content"]), "```", "",
        ]
    sc = _find("small_chain", want_marker=True)
    if sc:
        lines += [
            "**Copy-safety exhibit — gemma `small_chain` attempted content** "
            f"(`{sc['campaign']}/{sc['run']}`). The `[read_file#0]` tool-result "
            "marker leaked verbatim into the copied output (integration-blocking "
            "defect, logged; not fixed here):",
            "", "```", repr(sc["content"]), "```", "",
        ]
    return lines


def build_report(campaign_dirs: list[Path]) -> str:
    lines = ["# Stale-`out/` forensics — live vs stale-backed task outcomes", "",
             "Read-only reclassification of equality task-cells across campaigns. "
             "See `scripts/forensic_stale_out.py` for the exact rules.", "",
             "## Per-model × per-campaign classification", ""]
    all_exhibits: list = []
    for d in campaign_dirs:
        counts, exhibits = classify_campaign(d)
        all_exhibits += exhibits
        lines += _table(d.name, counts)
    lines += _appendix(all_exhibits)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="Stale-out/ forensic classifier.")
    parser.add_argument("campaigns", nargs="+", help="campaign output dirs")
    args = parser.parse_args(argv)
    print(build_report([Path(c) for c in args.campaigns]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
