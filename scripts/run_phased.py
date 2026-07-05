#!/usr/bin/env python
"""Phase-staged runner for integration run 1 (thin by construction).

One phase per invocation, gated and resumable — the run_sweep_staged pause
discipline applied to the phases of ONE flow. Provenance (``phases.jsonl`` +
``waivers.jsonl`` under the campaign ``output_dir``) is the source of truth for
state; nothing is held in memory across invocations.

    python scripts/run_phased.py <campaign.yaml> --status
    python scripts/run_phased.py <campaign.yaml> --next
    python scripts/run_phased.py <campaign.yaml> --waive "<reason>"
    python scripts/run_phased.py <campaign.yaml> --rerun-task <id> --oracle <phase>

The pure state/gate/report/resolution logic lives in module-level functions
(unit-tested); ``run_phase`` is the thin orchestration glue (paired-session
verified — the real flow is NOT executed by the test suite).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from agora.core.types import AgentRole  # noqa: E402
from agora.fleet.phase_gate import (  # noqa: E402
    TaskGateOutcome,
    evaluate_phase_gate,
    ordered_phases,
)

# ------------------------------------------------------------------ status model

PENDING, GREEN, RED, WAIVED = "pending", "green", "red", "waived"

#: Flow AgentRole → cast binding key. The flow expresses the verifier seat as
#: the read-only reviewer role; the cast names that seat "verifier".
ROLE_TO_CAST_KEY: dict[AgentRole, str] = {
    AgentRole.IMPLEMENTER: "implementer",
    AgentRole.REVIEWER: "verifier",
    AgentRole.TESTER: "tester",
    AgentRole.ARCHITECT: "planner",
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dicts; missing file → empty list."""
    if not Path(path).is_file():
        return []
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def latest_by_phase(records: list[dict[str, Any]]) -> dict[str, tuple[int, dict[str, Any]]]:
    """phase → (index, record) for the LAST-written record of each phase.

    Index is the record's position in ``phases.jsonl`` (write order); a re-run
    appends a higher-index record that supersedes the earlier one.
    """
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for idx, rec in enumerate(records):
        latest[rec["phase"]] = (idx, rec)  # later writes overwrite earlier
    return latest


def _waived_set(waivers: list[dict[str, Any]]) -> set[tuple[str, int]]:
    return {(w["phase"], int(w["record_index"])) for w in waivers}


def phase_states(
    flow_phases: list[str],
    records: list[dict[str, Any]],
    waivers: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Per-phase status in the flow's DECLARED order (never reordered by
    provenance write order)."""
    latest = latest_by_phase(records)
    waived = _waived_set(waivers)
    states: list[tuple[str, str]] = []
    for phase in flow_phases:
        if phase not in latest:
            states.append((phase, PENDING))
            continue
        idx, rec = latest[phase]
        if rec.get("passed"):
            states.append((phase, GREEN))
        elif (phase, idx) in waived:
            states.append((phase, WAIVED))
        else:
            states.append((phase, RED))
    return states


def next_action(states: list[tuple[str, str]]) -> tuple[str, str | None, str | None]:
    """Decide what ``--next`` does. Walks phases in order; stops at the first
    that is not green/waived.

    Returns ``(kind, phase, message)`` where kind is:
      - ``"run"``    → this pending phase is next to execute.
      - ``"refuse"`` → the frontier phase is red and unwaived (a red gate blocks
        every downstream phase until repaired or waived).
      - ``"done"``   → every phase is green or waived.
    """
    for phase, status in states:
        if status in (GREEN, WAIVED):
            continue
        if status == PENDING:
            return ("run", phase, None)
        return (
            "refuse",
            phase,
            f"{phase} gate is red and unwaived — repair with --rerun-task "
            f"or accept with --waive before advancing.",
        )
    return ("done", None, None)


def newest_red_gate(
    records: list[dict[str, Any]], waivers: list[dict[str, Any]]
) -> tuple[int, dict[str, Any]] | None:
    """The most-recently-written red record not already waived → (index, record)."""
    waived = _waived_set(waivers)
    for idx in range(len(records) - 1, -1, -1):
        rec = records[idx]
        if not rec.get("passed") and (rec["phase"], idx) not in waived:
            return (idx, rec)
    return None


# ------------------------------------------------------------------ gate building

def outcomes_from_results(
    tasks_by_id: dict[str, Any], task_results: list[Any]
) -> list[TaskGateOutcome]:
    """Map executed task results → phase-gate outcomes (blocking from the flow)."""
    outs: list[TaskGateOutcome] = []
    for r in task_results:
        task = tasks_by_id.get(r.task_id)
        blocking = bool(getattr(task, "blocking", True)) if task is not None else True
        pcs = [
            (name, bool(passed))
            for name, passed, _reason in getattr(r, "postcondition_results", [])
        ]
        outs.append(TaskGateOutcome(task_id=r.task_id, blocking=blocking, postconditions=pcs))
    return outs


def reevaluate_phase_gate(project_work_dir: Path, phase: str, flow_tasks: list[Any]) -> tuple[Any, dict[str, Any]]:
    """Re-evaluate a phase's gate MECHANICALLY over the current workspace — no LLM.

    Used by cross-phase repair: after ``--rerun-task <Y> --oracle <X>`` fixes a
    task in phase Y (e.g. src), the gate that must go green is phase X's (e.g.
    the pytest gate). This re-runs X's postconditions (run_check re-executes
    pytest; file_contains re-reads files) against the workspace and returns
    ``(PhaseGateResult, results_by_id)``.

    Seeding note: ``completions`` is seeded truthy (mark_complete is treated as
    already satisfied — the task completed in its prior run; we re-check its
    ARTIFACTS, not its completion signal) and ``artifacts`` is seeded with the
    real on-disk file list so ``file_exists`` reflects the workspace.
    """
    from types import SimpleNamespace

    wd = Path(project_work_dir)
    artifacts = (
        [str(p.relative_to(wd)).replace("\\", "/") for p in wd.rglob("*") if p.is_file()]
        if wd.is_dir() else []
    )
    outcomes: list[TaskGateOutcome] = []
    results_by_id: dict[str, Any] = {}
    for t in flow_tasks:
        if t.phase != phase:
            continue
        sink: list[dict[str, Any]] = []
        ctx = {
            "work_dir": str(wd), "artifacts": artifacts,
            "completions": [{"synthetic": True}], "progress_log": [],
            "run_check_sink": sink,
        }
        pcs = [(pred.name, bool(pred.evaluate(ctx)[0])) for pred in t.spec.postconditions]
        outcomes.append(TaskGateOutcome(task_id=t.id, blocking=t.blocking, postconditions=pcs))
        results_by_id[t.id] = SimpleNamespace(task_id=t.id, run_check_records=list(sink), nudges_used=0)
    # Flag the gate as mechanical so the ledger and --status distinguish an
    # artifact-state re-eval from a live task run.
    return replace(evaluate_phase_gate(phase, outcomes), mechanical=True), results_by_id


def _tail(text: str, limit: int = 500) -> str:
    """Last ``limit`` chars of ``text`` (the informative end of a traceback)."""
    text = text or ""
    return text if len(text) <= limit else "..." + text[-limit:]


def format_gate_report(
    gate: Any,
    results_by_id: dict[str, Any],
    harness: dict[str, Any] | None = None,
) -> str:
    """Human-readable phase-gate report: verdict, per-predicate outcomes,
    run_check stdout/stderr tails (with truncation flags), and nudge accounting.
    """
    lines: list[str] = []
    verdict = "GREEN" if gate.passed else "RED"
    lines.append(f"=== phase {gate.phase} gate: {verdict} ===")
    if gate.blockers:
        lines.append(f"  blockers: {', '.join(gate.blockers)}")
    nudge_budget = (harness or {}).get("nudge_budget", 0)
    nudges = sum(int(getattr(r, "nudges_used", 0) or 0) for r in results_by_id.values())
    lines.append(f"  nudge accounting: {nudges} fired (budget {nudge_budget} - v3.2 erratum: stall-recovery)")
    for out in gate.tasks:
        tag = "block" if out.blocking else "nonblock"
        mark = "PASS" if out.passed else "FAIL"
        lines.append(f"  [{mark}] {out.task_id} ({tag})")
        for name, passed in out.postconditions:
            lines.append(f"      {'ok ' if passed else 'FAIL'} {name}")
        rec_obj = results_by_id.get(out.task_id)
        for rc in getattr(rec_obj, "run_check_records", []) or []:
            cmd = " ".join(rc.get("cmd", []))
            ec = rc.get("exit_code")
            to = " TIMEOUT" if rc.get("timed_out") else ""
            lines.append(f"      run_check: {cmd} -> exit={ec}{to} passed={rc.get('passed')}")
            out_trunc = " [stdout truncated]" if rc.get("stdout_truncated") else ""
            err_trunc = " [stderr truncated]" if rc.get("stderr_truncated") else ""
            if rc.get("stdout"):
                lines.append(f"        stdout{out_trunc}: {_tail(rc['stdout'])}")
            if rc.get("stderr"):
                lines.append(f"        stderr{err_trunc}: {_tail(rc['stderr'])}")
    return "\n".join(lines)


# ------------------------------------------------------------------ cast / tasks

def resolve_agent_models(agents: list[Any], cast: Any, profiles: Any) -> tuple[list[Any], dict[str, Any]]:
    """Set each agent's model from the cast (by role → cast key → profile).

    Returns ``(agents_with_models, {model_id: ModelProfile})`` so the runner can
    build a per-model adapter factory. Raises if a role has no cast binding.
    """
    from agora.core.errors import AgoraError

    resolved: list[Any] = []
    model_to_profile: dict[str, Any] = {}
    for a in agents:
        key = ROLE_TO_CAST_KEY.get(a.role)
        binding = cast.bindings.get(key) if key else None
        if binding is None or not binding.profile:
            raise AgoraError(
                f"agent {a.name!r} (role {a.role}) has no profile binding in cast "
                f"{cast.name!r} (looked for key {key!r})"
            )
        profile = profiles.profiles[binding.profile]
        resolved.append(replace(a, model=profile.model))
        model_to_profile[profile.model] = profile
    return resolved, model_to_profile


def strip_cross_phase_deps(phase_task_ids: set[str], tasks: list[Any]) -> list[Any]:
    """Return ``tasks`` with every depends_on / order_after edge pointing OUTSIDE
    the phase dropped — prior phases already ran and left their artifacts on
    disk, so the orchestrator DAG for one phase must not reference absent tasks.
    Within-phase order_after (e.g. a verifier ordered after its blocking task) is
    preserved so the verifier still runs at gate time."""
    out: list[Any] = []
    for t in tasks:
        kept = tuple(d for d in t.depends_on if d in phase_task_ids)
        kept_order = tuple(o for o in getattr(t, "order_after", ()) if o in phase_task_ids)
        changed = kept != t.depends_on or kept_order != getattr(t, "order_after", ())
        out.append(replace(t, depends_on=kept, order_after=kept_order) if changed else t)
    return out


def build_repair_description(original_description: str, oracle_records: list[dict[str, Any]]) -> str:
    """Wrap a task's original prompt with the repair template shape: original
    text + the failed gate's oracle output VERBATIM (docs/integration/
    repair-task-template.md). Oracle = the run_check captures of the red gate."""
    parts = [
        original_description.strip(), "",
        "The following gate failed.", "",
        # F9 authority clause — the context-starved implementer re-read a
        # description consistent with its drifted file and no-op'd; name the
        # tests/spec as authoritative and the artifact as the thing to change.
        "The failing tests/spec below are AUTHORITATIVE. Your artifact violates "
        "them. Modify your artifact; do not dismiss the failures.", "",
        "Oracle output (verbatim):",
    ]
    for rc in oracle_records:
        cmd = " ".join(rc.get("cmd", []))
        parts.append(f"  $ {cmd}   (exit={rc.get('exit_code')}, timed_out={rc.get('timed_out')})")
        if rc.get("stdout"):
            trunc = " [truncated]" if rc.get("stdout_truncated") else ""
            parts.append(f"  stdout{trunc}:\n{rc['stdout']}")
        if rc.get("stderr"):
            trunc = " [truncated]" if rc.get("stderr_truncated") else ""
            parts.append(f"  stderr{trunc}:\n{rc['stderr']}")
    parts += ["", "Re-satisfy exactly this gate. Change only what the oracle points at."]
    return "\n".join(parts)


def oracle_records_for_phase(
    task_records: list[dict[str, Any]], phase: str
) -> list[dict[str, Any]]:
    """Resolve the verbatim oracle for ``phase`` from the PERSISTED per-phase
    TaskRecords (``tasks.jsonl``).

    For each failing BLOCKING task in the phase's newest records (reruns append,
    last write wins), returns: its failed run_check captures VERBATIM (cmd, exit,
    stdout/stderr, truncation flags — the real oracle) plus a synthetic entry per
    non-run_check failing predicate so the prompt still names it. Each entry is
    the run_check-record shape :func:`build_repair_description` templates.
    """
    latest: dict[str, dict[str, Any]] = {}
    for r in task_records:
        if (r.get("phase") or None) == phase:
            latest[r["task_id"]] = r  # last write wins (supersedes a prior attempt)
    oracle: list[dict[str, Any]] = []
    for _tid, r in latest.items():
        if not r.get("blocking", True) or r.get("status") == "passed":
            continue
        for rc in r.get("run_check_records") or []:
            if not rc.get("passed"):
                oracle.append(rc)  # verbatim stdout/stderr + truncation flags
        for pc in r.get("postconditions") or []:
            if not pc.get("passed") and not str(pc.get("name", "")).startswith("run_check"):
                oracle.append({
                    "cmd": [pc["name"]], "exit_code": 1, "timed_out": False,
                    "stdout": "", "stderr": f"predicate {pc['name']} failed",
                    "stdout_truncated": False, "stderr_truncated": False,
                })
    return oracle


# ------------------------------------------------------------------ persistence

def append_phase_record(phases_path: Path, gate: Any, run_id: str) -> None:
    """Append one PhaseGateRecord line (create the file if absent)."""
    from agora.observe.jsonl import PhaseGateRecord, PhaseTaskOutcome

    rec = PhaseGateRecord(
        run_id=run_id,
        phase=gate.phase,
        passed=bool(gate.passed),
        blockers=list(gate.blockers),
        mechanical=bool(getattr(gate, "mechanical", False)),
        tasks=[
            PhaseTaskOutcome(
                task_id=t.task_id, blocking=t.blocking, passed=t.passed,
                postconditions=[{"name": n, "passed": bool(p)} for n, p in t.postconditions],
            )
            for t in gate.tasks
        ],
    )
    with Path(phases_path).open("a", encoding="utf-8") as fh:
        fh.write(rec.model_dump_json() + "\n")


def record_waiver(waivers_path: Path, phase: str, record_index: int, reason: str) -> None:
    """Append one waiver line (provenance, not memory)."""
    with Path(waivers_path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"phase": phase, "record_index": record_index, "reason": reason}) + "\n")


def build_task_record(task: Any, result: Any, role: str, task_index: int, run_id: str) -> Any:
    """Build a :class:`TaskRecord` — the SAME shape campaign runs emit
    (run_check_records / nudges_used / phase / blocking included) — from an
    executed task + its TaskResult, using the observer's own classifiers."""
    from agora.observe.jsonl import (
        TaskRecord,
        classify_task_kind,
        derive_failure,
        derive_status,
    )

    output_path = getattr(task, "output_path", "") or ""
    spec = getattr(task, "spec", None)
    pc_names = [getattr(p, "name", "") for p in getattr(spec, "postconditions", ())]
    stage_kinds = [getattr(st, "kind", "llm") for st in getattr(task, "stages", ()) or ()]
    kind = classify_task_kind(
        output_path=output_path, postcondition_names=pc_names,
        stage_kinds=stage_kinds, role=role,
    )
    success = bool(getattr(result, "success", False))
    output = getattr(result, "output", "") or ""
    pc_results = list(getattr(result, "postcondition_results", []) or [])
    status = derive_status(success, output)
    malformed = int(getattr(result, "tool_calls_malformed", 0) or 0)
    unknown = int(getattr(result, "tool_call_unknown_name", 0) or 0)
    fc, fd = derive_failure(
        status=status, output=output, postcondition_results=pc_results,
        tool_calls_malformed=malformed, tool_call_unknown_name=unknown,
    )
    return TaskRecord(
        run_id=run_id, task_id=getattr(task, "id", ""), task_index=task_index, role=role,
        task_kind=kind, status=status, first_pass=success, loopback_count=0,
        iterations=int(getattr(result, "iterations", 0) or 0),
        postconditions=[{"name": n, "passed": bool(p)} for n, p, _ in pc_results],
        tool_calls_total=int(getattr(result, "tool_calls_total", 0) or 0),
        tool_calls_structured=int(getattr(result, "tool_calls_structured", 0) or 0),
        tool_calls_text_fallback=int(getattr(result, "tool_calls_text_fallback", 0) or 0),
        tool_calls_malformed=malformed, tool_call_unknown_name=unknown,
        tools_used=sorted(set(getattr(result, "tools_used", []) or [])),
        failure_category=fc, failure_detail=fd,
        artifact_capture=getattr(result, "artifact_capture", None),
        reviews_used=int(getattr(result, "reviews_used", 0) or 0),
        post_review_action=getattr(result, "post_review_action", None),
        nudges_used=int(getattr(result, "nudges_used", 0) or 0),
        phase=(getattr(task, "phase", "") or None),
        blocking=bool(getattr(task, "blocking", True)),
        run_check_records=list(getattr(result, "run_check_records", []) or []),
    )


def append_task_records(tasks_path: Path, records: list[Any]) -> None:
    """Append per-phase TaskRecords to ``tasks.jsonl`` (create if absent)."""
    with Path(tasks_path).open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(rec.model_dump_json() + "\n")


# ------------------------------------------------------------------ run.log

_RUN_LOG_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"


def attach_run_log(log_path: Path, phase_label: str = "") -> tuple[logging.Handler, int]:
    """Attach a FileHandler capturing the ``agora`` logger (INFO) to ``run.log``.

    This is the observer the phased runner was missing: campaign runs get
    ``run.log`` from the subprocess capture, but ``run_phased`` runs in-process,
    so nothing recorded the per-turn tool results — the T5.1 path-scope
    rejections ("result=ERROR: implementer role may not write ...") were
    invisible. Appends across phase invocations. Returns ``(handler, prev_level)``
    for :func:`detach_run_log`.
    """
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter(_RUN_LOG_FORMAT))
    handler.setLevel(logging.INFO)
    agora_logger = logging.getLogger("agora")
    prev_level = agora_logger.level
    agora_logger.setLevel(logging.INFO)
    agora_logger.addHandler(handler)
    if phase_label:
        agora_logger.info("=== run_phased %s ===", phase_label)
    return handler, prev_level


def detach_run_log(state: tuple[logging.Handler, int]) -> None:
    """Flush + remove the run.log handler and restore the prior logger level."""
    handler, prev_level = state
    agora_logger = logging.getLogger("agora")
    agora_logger.removeHandler(handler)
    try:
        handler.flush()
        handler.close()
    except OSError:
        pass
    agora_logger.setLevel(prev_level)


# ------------------------------------------------------------------ ollama health

def ollama_missing_models(tags: dict[str, Any], required: list[str]) -> list[str]:
    """Return the required model tags (``name:tag``) absent from ``/api/tags``."""
    present = {m.get("name", "") for m in (tags.get("models") or [])}
    return [m for m in required if m not in present]


def ollama_health_or_die(base_url: str, required: list[str]) -> None:
    """Fail loudly (SystemExit) if the daemon is down or a model is missing
    (OLLAMA.md: bare `serve` from D:\\ollama\\models, one model at a time)."""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/version", timeout=3) as resp:
            json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"[FATAL] Ollama daemon unreachable at {base_url} ({type(exc).__name__}). "
            f"Start it per OLLAMA.md (bare `ollama serve`, OLLAMA_MODELS=D:\\ollama\\models)."
        ) from exc
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=5) as resp:
            tags = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[FATAL] Ollama /api/tags failed: {exc}") from exc
    missing = ollama_missing_models(tags, required)
    if missing:
        raise SystemExit(
            f"[FATAL] required model(s) not present in Ollama: {missing}. "
            f"Pull them or check OLLAMA_MODELS points at D:\\ollama\\models."
        )


# ------------------------------------------------------------------ campaign load

def load_campaign(path: str | Path) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"[FATAL] campaign {path} must be a mapping")
    for key in ("cast", "flow", "harness", "output_dir"):
        if key not in data:
            raise SystemExit(f"[FATAL] campaign {path} missing required key {key!r}")
    return data


def _flow_phases(flow_path: str) -> tuple[list[str], list[Any], list[Any]]:
    """Return (ordered_phases, agents, tasks) for a flow (ids preserved)."""
    from agora.core.flow import instantiate_flow, load_flow

    flow = load_flow(flow_path)
    agents, tasks = instantiate_flow(flow, "echobot", id_strategy="preserve")
    return ordered_phases(tasks), agents, tasks


# ------------------------------------------------------------------ orchestration glue

async def run_phase(campaign: dict[str, Any], phase: str, *, run_id: str = "r001",
                    rerun_task: str | None = None, oracle_phase: str | None = None) -> Any:
    """Execute one phase's tasks via the cast-loaded orchestrator.

    Returns ``(gate, results_by_id, task_records)`` — the PhaseGateResult, the
    in-memory TaskResults (for the console report), and the persistable
    TaskRecords (appended to tasks.jsonl at phase completion). Thin glue —
    paired-session verified (not run by tests)."""
    from agora.core.flow import instantiate_flow, load_flow
    from agora.fleet.cast import load_cast, resolve_cast
    from agora.fleet.profiles import build_llm_factory, load_profiles
    from agora.plan.harness import HarnessConfig, build_matrix_client, build_orchestrator

    profiles = load_profiles("profiles.yaml")
    cast = load_cast(campaign["cast"])
    # Health check per OLLAMA.md — the models the cast will actually load.
    required = [
        rb.model.removeprefix("ollama/")
        for rb in resolve_cast(cast, profiles)
        if not rb.is_human and rb.model.startswith("ollama/") and rb.resident
    ]
    base_url = "http://localhost:11434"
    ollama_health_or_die(base_url, required)

    flow = load_flow(campaign["flow"])
    agents, tasks = instantiate_flow(flow, "echobot", id_strategy="preserve")
    agents, model_to_profile = resolve_agent_models(agents, cast, profiles)

    phase_tasks = [t for t in tasks if t.phase == phase]
    if rerun_task:
        # Re-run the named task PLUS any same-phase verifier ordered after it —
        # the F5 fix means verifiers run at their phase even when the task they
        # observe failed, so a re-establishment of the phase must include them
        # (this is what lets V5.1 produce a verdict on a red P5 re-run).
        keep = {rerun_task} | {
            t.id for t in phase_tasks
            if not t.blocking and rerun_task in getattr(t, "order_after", ())
        }
        phase_tasks = [t for t in phase_tasks if t.id in keep]
        if oracle_phase:
            # Oracle is resolved from the PERSISTED per-phase TaskRecords, so the
            # repair prompt carries the red gate's run_check stdout/stderr verbatim.
            # Only the reran task carries the oracle; verifiers keep their prompt.
            task_records = load_jsonl(Path(campaign["output_dir"]) / "tasks.jsonl")
            oracle = oracle_records_for_phase(task_records, oracle_phase)
            phase_tasks = [
                replace(t, description=build_repair_description(t.description, oracle))
                if t.id == rerun_task else t
                for t in phase_tasks
            ]
    phase_ids = {t.id for t in phase_tasks}
    phase_tasks = strip_cross_phase_deps(phase_ids, phase_tasks)
    used_agent_names = {t.agent_id for t in phase_tasks}
    phase_agents = [a for a in agents if a.name in used_agent_names]

    harness = campaign["harness"]
    cfg = HarnessConfig(
        tool_errors=harness.get("tool_errors", "corrective"),
        nudge_budget=int(harness.get("nudge_budget", 1)),
        review_budget=int(harness.get("review_budget", 0)),
        work_dir=Path(campaign["output_dir"]) / "echobot",
        review_timeout_seconds=float(campaign.get("run", {}).get("review_timeout_seconds", 5)),
        enable_observer=False,
    )

    def llm_factory(model_ref: str, _m2p=model_to_profile, _prof=profiles):
        prof = _m2p.get(model_ref) or _prof.select()
        return build_llm_factory(prof)(model_ref)

    # Attach run.log (per-turn tool results + path-scope rejections) for this
    # phase invocation — the observer campaign runs get for free from subprocess
    # capture, which the in-process phased runner otherwise lacks.
    label = f"{phase}{' rerun ' + rerun_task if rerun_task else ''}"
    log_state = attach_run_log(Path(campaign["output_dir"]) / "run.log", label)
    client = await build_matrix_client(cfg)
    try:
        orch = build_orchestrator(cfg, client, agents[0].model, llm_factory=llm_factory)
        result = await orch.run_project("echobot", phase_agents, phase_tasks, max_loopbacks=0)
    finally:
        await client.close()
        detach_run_log(log_state)

    tasks_by_id = {t.id: t for t in phase_tasks}
    # Cross-phase repair: the reran task lives in phase ``phase`` (its owner),
    # but --oracle names a DIFFERENT phase whose gate must be re-checked. Fixing
    # a src task (Y) to satisfy the pytest gate (X) → re-evaluate X's gate
    # mechanically over the now-modified workspace. Same-phase repair keeps the
    # normal evaluate-over-the-reran-task path.
    if rerun_task and oracle_phase and oracle_phase != phase:
        project_dir = Path(campaign["output_dir"]) / "echobot" / "echobot"
        gate, gate_results_by_id = reevaluate_phase_gate(project_dir, oracle_phase, tasks)
    else:
        outcomes = outcomes_from_results(tasks_by_id, result.task_results)
        gate = evaluate_phase_gate(phase, outcomes)
        gate_results_by_id = {r.task_id: r for r in result.task_results}
    role_by_agent = {a.name: getattr(a.role, "value", str(a.role)) for a in agents}
    task_records = [
        build_task_record(
            tasks_by_id[res.task_id], res, role_by_agent.get(tasks_by_id[res.task_id].agent_id, ""),
            i, run_id,
        )
        for i, res in enumerate(result.task_results)
        if res.task_id in tasks_by_id
    ]
    return gate, gate_results_by_id, task_records


# ------------------------------------------------------------------ CLI

def _print_status(campaign: dict[str, Any]) -> None:
    phases, _agents, _tasks = _flow_phases(campaign["flow"])
    out_dir = Path(campaign["output_dir"])
    records = load_jsonl(out_dir / "phases.jsonl")
    waivers = load_jsonl(out_dir / "waivers.jsonl")
    latest = latest_by_phase(records)
    print(f"=== {campaign.get('name', 'run')} — phase status ===")
    for phase, status in phase_states(phases, records, waivers):
        marker = ""
        if phase in latest and latest[phase][1].get("mechanical"):
            marker = "  (mechanical re-eval)"
        print(f"  {phase:6} {status}{marker}")
    kind, ph, msg = next_action(phase_states(phases, records, waivers))
    if kind == "run":
        print(f"next: run {ph}")
    elif kind == "refuse":
        print(f"next: BLOCKED - {msg}")
    else:
        print("next: done (all phases green or waived)")


def _do_waive(campaign: dict[str, Any], reason: str) -> None:
    out_dir = Path(campaign["output_dir"])
    records = load_jsonl(out_dir / "phases.jsonl")
    waivers = load_jsonl(out_dir / "waivers.jsonl")
    target = newest_red_gate(records, waivers)
    if target is None:
        raise SystemExit("[refuse] no unwaived red gate to waive.")
    idx, rec = target
    record_waiver(out_dir / "waivers.jsonl", rec["phase"], idx, reason)
    print(f"waived {rec['phase']} gate (record #{idx}): {reason}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase-staged runner (integration run 1).")
    p.add_argument("campaign", help="Path to the run-1 campaign YAML.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true", help="Print per-phase state and exit.")
    g.add_argument("--next", action="store_true", dest="do_next", help="Run the next pending phase.")
    g.add_argument("--waive", metavar="REASON", help="Record a waiver on the newest red gate.")
    g.add_argument("--rerun-task", metavar="ID", dest="rerun_task", help="Repair-rerun one task.")
    p.add_argument("--oracle", metavar="PHASE", help="Gate ref (phase) whose oracle wraps the rerun.")
    args = p.parse_args(argv)

    from agora.plan.harness import force_utf8_stdio

    force_utf8_stdio()
    campaign = load_campaign(args.campaign)
    out_dir = Path(campaign["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(campaign.get("run", {}).get("id", "r001"))

    if args.status:
        _print_status(campaign)
        return 0
    if args.waive:
        _do_waive(campaign, args.waive)
        return 0

    import asyncio

    phases, _agents, _tasks = _flow_phases(campaign["flow"])
    records = load_jsonl(out_dir / "phases.jsonl")
    waivers = load_jsonl(out_dir / "waivers.jsonl")

    if args.rerun_task:
        if not args.oracle:
            raise SystemExit("--rerun-task requires --oracle <phase>")
        # The rerun's phase is the one that owns the task.
        _phases, _ag, tasks = _flow_phases(campaign["flow"])
        owner = next((t.phase for t in tasks if t.id == args.rerun_task), None)
        if owner is None:
            raise SystemExit(f"unknown task id {args.rerun_task!r}")
        gate, results_by_id, task_records = asyncio.run(
            run_phase(campaign, owner, run_id=run_id, rerun_task=args.rerun_task,
                      oracle_phase=args.oracle)
        )
        append_task_records(out_dir / "tasks.jsonl", task_records)
        append_phase_record(out_dir / "phases.jsonl", gate, run_id)
        print(format_gate_report(gate, results_by_id, campaign.get("harness")))
        return 0 if gate.passed else 1

    # --next
    kind, phase, msg = next_action(phase_states(phases, records, waivers))
    if kind == "refuse":
        raise SystemExit(f"[refuse] {msg}")
    if kind == "done":
        print("done - all phases green or waived.")
        return 0
    gate, results_by_id, task_records = asyncio.run(run_phase(campaign, phase, run_id=run_id))
    append_task_records(out_dir / "tasks.jsonl", task_records)
    append_phase_record(out_dir / "phases.jsonl", gate, run_id)
    print(format_gate_report(gate, results_by_id, campaign.get("harness")))
    return 0 if gate.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
