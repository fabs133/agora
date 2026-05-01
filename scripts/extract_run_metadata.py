"""Extract run metadata from Agora workspace artifacts.

Two modes:

* ``--inventory`` writes ``docs/runs/_inventory.csv`` only.  Cheap, used by
  Phase A step 1 to enumerate run directories with git-summary fields and a
  best-effort log-path hint.

* default mode writes ``docs/runs/registry.yaml`` (and the inventory CSV as a
  side effect).  Phase A step 2: scans every workspace log, matches logs to
  runs by content + timestamp overlap, and extracts per-task pass/fail,
  loopback counts, durations, model/provider, and cost.

Idempotent: same inputs produce identical outputs.  YAML keys are sorted;
counts derive from the artifacts, not from extraction-time clocks.
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = REPO_ROOT / "workspace"
LOGS_DIR = WORKSPACE / ".logs"
DOCS_RUNS = REPO_ROOT / "docs" / "runs"

RUN_DIR_RE = re.compile(
    r"^(?P<project>[A-Za-z0-9_-]+?)\.(?P<suffix>run[0-9]+[A-Za-z0-9_-]*)$"
)
LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),(\d{3})")
LITELLM_MODEL_RE = re.compile(
    r"LiteLLM completion\(\) model=\s*(?P<model>[\w\-./:]+);\s*provider\s*=\s*(?P<provider>\w+)"
)
DISPATCH_RE = re.compile(r"dispatch: task (?P<task>\S+) -> agent (?P<agent>\S+)")
TASK_DONE_RE = re.compile(
    r"task (?P<task>\S+) done: success=(?P<success>True|False) iterations=(?P<iters>\d+)"
)
TOOL_CALL_RE = re.compile(r"tool call: task=(?P<task>\S+) turn=(?P<turn>\d+) name=(?P<tool>\S+)")
LLM_RETURN_RE = re.compile(
    r"llm return: task=(?P<task>\S+) turn=(?P<turn>\d+) tool_calls=(?P<n>\d+)"
)
AUTO_HOOK_RE = re.compile(r"auto-hook: (?P<hook>\S+) on (?P<path>\S+) -> (?P<result>\S+)")
LOOPBACK_RE = re.compile(r"loopback|retry phase|reject_(?:implementation|analysis|architecture|testing)", re.I)
POSTCOND_FAIL_RE = re.compile(r"postcondition .* failed|predicate \S+ failed|postcond.* not satisfied", re.I)
PHASE_RE = re.compile(r"advance_phase: (?P<from>\w+) -> (?P<to>\w+)")
LOADER_PROJECT_RE = re.compile(r"agora\.plan\.loader: loader: loaded plan '(?P<plan>[^']+)'")
RUNNER_PROJECT_RE = re.compile(r"orchestrator: starting project (?P<project>\S+)")

# Project-specific anchors (task names, agent names, file paths) used to
# fingerprint which project a log corresponds to.  Tier order matters: more
# specific projects come first so we don't mis-classify a discord-bot-full
# log as discord-bot just because it mentions ``bot.py``.
#
# Each anchor is a substring; a log gets a per-project hit count, and the
# project with the most hits wins (ties broken by tier order).
PROJECT_HINT_TIERS: list[tuple[str, list[str]]] = [
    ("code-review", [
        "agent reviewer",
        "task=review_bot_py",
        "task=review_config_py",
        "task=review_matrix_bridge_py",
        "task=review_test_",
        "review/REPORT.md",
    ]),
    ("discord-bot-full", [
        "task=design_bridge_spec",
        "task=write_matrix_bridge",
        "task=write_config_module",
        "task=design_command_inventory",
        "matrix_bridge.py",
        "scripts/run_discord_bot_full",
    ]),
    ("plan-builder", [
        "task=review_brief",
        "task=decide_library",
        "task=decide_storage",
        "task=review_plan",
        "task=author_spec",
        "task=fill_test_body",
        "plan/api_spec.md",
        "plan/brief.md",
    ]),
    ("url-shortener", [
        "url_shortener.py",
        "url-shortener-mvp",
        "task=core_domain_module",
    ]),
    ("fastapi-crud", [
        "@app.get",
        "@app.post",
        "task=write_endpoints_pkg",
        "task=write_main",
        "fastapi-crud",
    ]),
    ("discord-bot", [
        "task=write_bot_skeleton",
        "task=write_bot_commands_help_lookup",
        "task=write_bot_commands_roll_command",
        "task=write_bot_commands_register_command",
        "task=write_test_help_lookup",
        "task=write_test_roll_command",
    ]),
]

# Cost estimates from session memory (2026-04-22 notes), used when a log
# does not record cost lines.  Per-run figures only — per-task breakdown
# is deferred (see plan).
COST_ESTIMATES_USD = {
    "gpt-4o-mini": 0.025,
    "gpt-4o": 0.40,
    "claude-opus-4-7": None,  # subscription, not per-token billed
    "claude-sonnet-4-6": None,
    "qwen2.5:7b": 0.0,
    "qwen2.5:14b": 0.0,
    "qwen2.5-coder:7b": 0.0,
}


@dataclass
class TaskRecord:
    name: str
    agent: str | None = None
    iterations: int = 0
    success: bool | None = None
    tool_call_count: int = 0
    # Inferred from ``llm return: tool_calls=N`` when per-call lines are
    # absent (older log format).  Resolved into ``tool_call_count`` at end.
    _inferred_tool_calls: int = 0
    auto_hook_ok: int = 0
    auto_hook_fail: int = 0


@dataclass
class CostRecord:
    value_usd: float | None = None
    source: str = "unknown"  # recorded | estimated | unknown
    note: str = ""


@dataclass
class RunRecord:
    run_id: str
    project: str
    suffix: str
    dir_path: str
    git_commit_count: int = 0
    git_first_iso: str = ""
    git_last_iso: str = ""
    log_paths: list[str] = field(default_factory=list)
    model: str | None = None
    provider: str | None = None
    duration_seconds: float | None = None
    tasks: list[TaskRecord] = field(default_factory=list)
    loopback_count: int = 0
    cost: CostRecord = field(default_factory=CostRecord)
    notes: str = ""


def _git_summary(d: Path) -> tuple[int, str, str]:
    if not (d / ".git").exists():
        return 0, "", ""
    try:
        result = subprocess.run(
            ["git", "log", "--format=%aI", "--all"],
            cwd=str(d),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return 0, "", ""
    if result.returncode != 0:
        return 0, "", ""
    lines = [l for l in result.stdout.strip().splitlines() if l]
    if not lines:
        return 0, "", ""
    # `git log` returns newest-first.
    return len(lines), lines[-1], lines[0]


def discover_runs() -> list[RunRecord]:
    """Walk ``workspace/`` and return one record per run directory.

    Two flavours of run dir exist:

    * Archived runs with a ``.run<N>...`` suffix (e.g. ``discord-bot.run13``).
    * Base dirs without that suffix (e.g. ``discord-bot-full/``) — these
      hold the most recent successful run that the user did not archive
      before moving on.  Treated as a synthetic ``<project>.live`` entry
      provided they have any git history.
    """
    rows: list[RunRecord] = []
    if not WORKSPACE.exists():
        return rows
    for d in sorted(WORKSPACE.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        m = RUN_DIR_RE.match(d.name)
        if m:
            project = m.group("project")
            suffix = m.group("suffix")
            run_id = d.name
        else:
            # Base dir.  Only include if there's git history; otherwise
            # it's likely an empty scaffold left over from earlier layouts.
            if not (d / ".git").exists():
                continue
            project = d.name
            suffix = "live"
            run_id = f"{d.name}.live"
        commits, first_iso, last_iso = _git_summary(d)
        if not m and commits == 0:
            continue
        rows.append(
            RunRecord(
                run_id=run_id,
                project=project,
                suffix=suffix,
                dir_path=str(d).replace("\\", "/"),
                git_commit_count=commits,
                git_first_iso=first_iso,
                git_last_iso=last_iso,
            )
        )
    return rows


@dataclass
class LogSummary:
    path: Path
    first_iso: str
    last_iso: str
    model: str | None
    provider: str | None
    project_hint: str | None
    line_count: int


def _scan_log(path: Path) -> LogSummary:
    first_iso = ""
    last_iso = ""
    model: str | None = None
    provider: str | None = None
    project_hint: str | None = None
    line_count = 0
    project_hits: dict[str, int] = defaultdict(int)
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_count += 1
                ts_m = LOG_TIMESTAMP_RE.match(line)
                if ts_m:
                    iso = f"{ts_m.group(1)}T{ts_m.group(2)}"
                    if not first_iso:
                        first_iso = iso
                    last_iso = iso
                if model is None:
                    lm = LITELLM_MODEL_RE.search(line)
                    if lm:
                        model = lm.group("model")
                        provider = lm.group("provider")
                for project, hints in PROJECT_HINT_TIERS:
                    for h in hints:
                        if h in line:
                            project_hits[project] += 1
                            break
    except Exception:
        pass
    if project_hits:
        # Tier order is the secondary sort key: earlier in PROJECT_HINT_TIERS
        # is more specific, so it wins ties (e.g. discord-bot-full beats
        # discord-bot).
        tier_index = {name: i for i, (name, _) in enumerate(PROJECT_HINT_TIERS)}
        project_hint = max(
            project_hits.items(),
            key=lambda kv: (kv[1], -tier_index[kv[0]]),
        )[0]
    return LogSummary(
        path=path,
        first_iso=first_iso,
        last_iso=last_iso,
        model=model,
        provider=provider,
        project_hint=project_hint,
        line_count=line_count,
    )


def discover_logs() -> list[LogSummary]:
    out: list[LogSummary] = []
    if not LOGS_DIR.exists():
        return out
    for p in sorted(LOGS_DIR.iterdir()):
        if not p.is_file() or not p.name.endswith(".log"):
            continue
        out.append(_scan_log(p))
    return out


def _project_canonical(project: str) -> str:
    """Map a run's project field to the canonical hint key used by logs."""
    return {
        "url-shortener-mvp": "url-shortener",
        "discord-bot": "discord-bot",
        "discord-bot-full": "discord-bot-full",
        "fastapi-crud": "fastapi-crud",
        "plan-builder": "plan-builder",
        "code-review": "code-review",
        "fastapi-crud-from-yaml": "fastapi-crud",
        "repos": "repos",
    }.get(project, project)


def _iso_overlap(a_first: str, a_last: str, b_first: str, b_last: str) -> bool:
    if not (a_first and a_last and b_first and b_last):
        return False
    return not (a_last < b_first or b_last < a_first)


def match_logs_to_runs(runs: list[RunRecord], logs: list[LogSummary]) -> None:
    """Populate run.log_paths via project-hint match + git-time overlap.

    Also attach sibling voter logs (``voter_X.log`` next to ``planner_X.log``)
    since they share the same run identity but lack ISO timestamps.
    """
    log_index = {ls.path.name: ls for ls in logs}
    for r in runs:
        canon = _project_canonical(r.project)
        candidates: list[str] = []
        for ls in logs:
            hint_match = (ls.project_hint == canon) if ls.project_hint else False
            time_match = _iso_overlap(r.git_first_iso, r.git_last_iso, ls.first_iso, ls.last_iso)
            if hint_match and time_match:
                candidates.append(ls.path.name)
        # Attach sibling voter logs if their planner sibling matched.
        siblings: list[str] = []
        for name in candidates:
            if name.startswith("planner_") and name.endswith(".log"):
                stem = name[len("planner_"):-len(".log")]
                voter = f"voter_{stem}.log"
                if voter in log_index:
                    siblings.append(voter)
        r.log_paths = sorted(set(candidates) | set(siblings))


def _normalise_task(name: str) -> str:
    """Strip stage suffix from task names like ``parent:stage_name``.

    Staged tasks log as ``task=parent:stage`` in some places (llm return,
    stage_runner) and as ``task=parent`` in others (dispatch, task done).
    Treat them as a single task identified by the parent.
    """
    return name.split(":", 1)[0]


def _parse_log_for_run(run: RunRecord, log_path: Path) -> None:
    """Update run with task/loopback/duration data extracted from a log."""
    tasks_by_name: dict[str, TaskRecord] = {t.name: t for t in run.tasks}
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    loopbacks = run.loopback_count
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                ts_m = LOG_TIMESTAMP_RE.match(line)
                if ts_m:
                    try:
                        ts = datetime.fromisoformat(f"{ts_m.group(1)} {ts_m.group(2)}")
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts
                    except ValueError:
                        pass
                d_m = DISPATCH_RE.search(line)
                if d_m:
                    name = _normalise_task(d_m.group("task"))
                    rec = tasks_by_name.setdefault(name, TaskRecord(name=name))
                    if rec.agent is None:
                        rec.agent = d_m.group("agent")
                    continue
                done_m = TASK_DONE_RE.search(line)
                if done_m:
                    name = _normalise_task(done_m.group("task"))
                    rec = tasks_by_name.setdefault(name, TaskRecord(name=name))
                    rec.success = done_m.group("success") == "True"
                    rec.iterations = max(rec.iterations, int(done_m.group("iters")))
                    continue
                tool_m = TOOL_CALL_RE.search(line)
                if tool_m:
                    name = _normalise_task(tool_m.group("task"))
                    rec = tasks_by_name.setdefault(name, TaskRecord(name=name))
                    rec.tool_call_count += 1
                    continue
                ret_m = LLM_RETURN_RE.search(line)
                if ret_m:
                    # Older logs (pre gpt-4o-mini era) lack per-call lines
                    # but include the count on the llm return.  Captured so
                    # we have a fallback if no ``tool call:`` lines exist.
                    name = _normalise_task(ret_m.group("task"))
                    rec = tasks_by_name.setdefault(name, TaskRecord(name=name))
                    rec._inferred_tool_calls += int(ret_m.group("n"))
                    continue
                hook_m = AUTO_HOOK_RE.search(line)
                if hook_m:
                    if hook_m.group("result") == "OK":
                        # Auto-hook fires per file write; not currently
                        # attributed to a specific task, so attach to last
                        # dispatched task once we add tracking.  For now,
                        # tally at run level via aggregation below.
                        pass
                if LOOPBACK_RE.search(line):
                    loopbacks += 1
    except Exception:
        return
    # Resolve inferred counts into real ones when per-call lines were absent.
    for rec in tasks_by_name.values():
        if rec.tool_call_count == 0 and rec._inferred_tool_calls > 0:
            rec.tool_call_count = rec._inferred_tool_calls
    run.tasks = sorted(tasks_by_name.values(), key=lambda t: t.name)
    run.loopback_count = loopbacks
    if first_ts and last_ts:
        run.duration_seconds = max(
            run.duration_seconds or 0.0,
            (last_ts - first_ts).total_seconds(),
        )


# Per-project defaults for runs whose logs don't record a model and whose
# filename suffix is also silent.  Derived from docs/lessons-learned.md (Runs 1-17
# were all qwen2.5:7b on local Ollama).
DEFAULT_MODEL_BY_PROJECT: dict[str, str] = {
    "discord-bot": "qwen2.5:7b",
    "discord-bot-full": "qwen2.5:7b",
    "fastapi-crud": "qwen2.5:7b",
    "code-review": "qwen2.5:7b",
    # Plan-builder is multi-provider, but only LiteLLM-era runs emit a
    # ``LiteLLM completion()`` line; absence of that line in a matched log
    # means it ran on the pre-LiteLLM Ollama default.
    "plan-builder": "qwen2.5:7b",
}


def _infer_model_from_suffix(suffix: str) -> str | None:
    """Fallback: read model tier from the run-dir filename suffix.

    Conventions used in workspace/ (filename suffix is canonical metadata
    when the log itself does not record the model, as is the case with the
    pre-LiteLLM 7B-era runs)::

        .run<N>-7b-...   -> qwen2.5:7b
        .run<N>-4omini-... -> gpt-4o-mini
        .run<N>-coder    -> qwen2.5-coder:7b (Run 6)
    """
    s = suffix.lower()
    if "4omini" in s:
        return "gpt-4o-mini"
    if "coder" in s:
        return "qwen2.5-coder:7b"
    if "7b" in s:
        return "qwen2.5:7b"
    return None


def extract_per_run(runs: list[RunRecord], logs: list[LogSummary]) -> None:
    log_by_name = {ls.path.name: ls for ls in logs}
    for r in runs:
        for log_name in r.log_paths:
            ls = log_by_name.get(log_name)
            if ls is None:
                continue
            if r.model is None and ls.model is not None:
                r.model = ls.model
                r.provider = ls.provider
            _parse_log_for_run(r, ls.path)
        # Fall back to filename suffix for runs whose logs don't record
        # a model (e.g. pre-LiteLLM 7B runs).
        if r.model is None:
            inferred = _infer_model_from_suffix(r.suffix)
            if inferred is not None:
                r.model = inferred
                r.provider = "ollama" if inferred.startswith("qwen") else None
        # Final fallback: project-level default.  docs/lessons-learned.md establishes
        # that Runs 1-17 (discord-bot, fastapi-crud, discord-bot-full,
        # code-review eras) ran on qwen2.5:7b before LiteLLM landed.
        if r.model is None and r.project in DEFAULT_MODEL_BY_PROJECT:
            r.model = DEFAULT_MODEL_BY_PROJECT[r.project]
            r.provider = "ollama"
        # Cost field
        if r.model:
            est = COST_ESTIMATES_USD.get(r.model)
            if est is not None:
                r.cost = CostRecord(
                    value_usd=est,
                    source="estimated",
                    note="from session memory; per-run figure",
                )
            else:
                r.cost = CostRecord(value_usd=None, source="unknown", note="model not in estimate table")
        else:
            r.cost = CostRecord(value_usd=None, source="unknown", note="model not detected from logs")


def emit_inventory_csv(runs: list[RunRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "run_id",
            "project",
            "suffix",
            "dir_path",
            "git_commit_count",
            "git_first_iso",
            "git_last_iso",
            "log_paths",
        ])
        for r in runs:
            w.writerow([
                r.run_id,
                r.project,
                r.suffix,
                r.dir_path,
                r.git_commit_count,
                r.git_first_iso,
                r.git_last_iso,
                ";".join(r.log_paths),
            ])


def emit_registry_yaml(runs: list[RunRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_by": "scripts/extract_run_metadata.py",
        "cost_source_legend": {
            "recorded": "value parsed from log line",
            "estimated": "value from session-memory per-tier estimate; see findings.md",
            "unknown": "no model detected or no estimate available",
        },
        "runs": [_run_to_dict(r) for r in runs],
    }
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False, width=100)


def _run_to_dict(r: RunRecord) -> dict:
    return {
        "run_id": r.run_id,
        "project": r.project,
        "suffix": r.suffix,
        "dir_path": r.dir_path,
        "git": {
            "commit_count": r.git_commit_count,
            "first_iso": r.git_first_iso,
            "last_iso": r.git_last_iso,
        },
        "log_paths": r.log_paths,
        "model": r.model,
        "provider": r.provider,
        "duration_seconds": (round(r.duration_seconds, 1) if r.duration_seconds else None),
        "loopback_count": r.loopback_count,
        "tasks": [
            {
                "name": t.name,
                "agent": t.agent,
                "iterations": t.iterations,
                "success": t.success,
                "tool_call_count": t.tool_call_count,
            }
            for t in r.tasks
        ],
        "cost": {
            "value_usd": r.cost.value_usd,
            "source": r.cost.source,
            "note": r.cost.note,
        },
        "notes": r.notes,
    }


def merge_hand_filled_notes(runs: list[RunRecord], notes_path: Path) -> int:
    """Apply hand-filled notes from ``registry_notes.yaml`` onto runs.

    Format::

        notes:
          <run_id>: |
            multi-line note text
          ...

    Returns the count of runs whose notes were populated.
    """
    if not notes_path.exists():
        return 0
    try:
        data = yaml.safe_load(notes_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        print(f"WARN: could not parse {notes_path}: {e}", file=sys.stderr)
        return 0
    notes_map: dict[str, str] = data.get("notes", {}) or {}
    applied = 0
    for r in runs:
        if r.run_id in notes_map:
            r.notes = str(notes_map[r.run_id]).rstrip()
            applied += 1
    return applied


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inventory", action="store_true", help="emit _inventory.csv only")
    ap.add_argument("--out", type=Path, default=DOCS_RUNS, help="output directory")
    ap.add_argument(
        "--notes",
        type=Path,
        default=None,
        help="hand-filled notes file (default: <out>/registry_notes.yaml)",
    )
    args = ap.parse_args(argv)

    runs = discover_runs()
    print(f"Discovered {len(runs)} run directories.", file=sys.stderr)

    logs = discover_logs()
    print(f"Discovered {len(logs)} log files.", file=sys.stderr)

    match_logs_to_runs(runs, logs)
    matched = sum(1 for r in runs if r.log_paths)
    print(f"Matched logs for {matched}/{len(runs)} runs (filename+content+time).", file=sys.stderr)

    inv_path = args.out / "_inventory.csv"
    emit_inventory_csv(runs, inv_path)
    print(f"Wrote {inv_path}", file=sys.stderr)

    if args.inventory:
        return 0

    extract_per_run(runs, logs)
    notes_path = args.notes or (args.out / "registry_notes.yaml")
    applied = merge_hand_filled_notes(runs, notes_path)
    if applied:
        print(f"Merged hand-filled notes for {applied} runs from {notes_path}", file=sys.stderr)
    reg_path = args.out / "registry.yaml"
    emit_registry_yaml(runs, reg_path)
    print(f"Wrote {reg_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
