"""Structured run logging — JSONL schema v1.

Every Agora run that wires a :class:`RunObserver` emits two files into a
per-run output directory:

- ``run.jsonl``   — a single :class:`RunRecord` line (the whole-run summary).
- ``tasks.jsonl`` — one :class:`TaskRecord` line per task (execution order).

Both records carry ``schema_version = 1``. The schema is **locked** (Phase A,
Artifact 1) — fields, names, and closed vocabularies are pinned here as
pydantic models so downstream analysis can validate every line it reads.

The observer is intentionally decoupled from the orchestrator's own types: it
exposes two low-level emit methods (:meth:`RunObserver.record_task` and
:meth:`RunObserver.record_run`) that take plain values, so a test can drive it
with a fake orchestrator. The orchestrator-facing convenience
(:meth:`RunObserver.record_task_from_result`) does the derivation from a
:class:`~agora.fleet.agent_runtime.TaskResult`.

Tool-call accounting semantics. The three primary counters share one unit —
**tool calls** — so they reconcile arithmetically:

    tool_calls_structured + tool_calls_text_fallback == tool_calls_total

- ``tool_calls_total``         — total model-emitted tool calls executed on
  the task, summed across stages and attempts (synthetic auto-hook calls are
  excluded). A *call* count.
- ``tool_calls_structured``    — calls that arrived via the assistant message's
  native ``tool_calls`` field. A *call* count.
- ``tool_calls_text_fallback`` — calls extracted by the Ollama adapter's
  ``_parse_tool_calls_from_text`` from prose ``content``. A *call* count, and
  the headline tool-call-fidelity signal. (The adapter's fallback only runs
  when the structured field was empty, so a given turn's calls are wholly one
  origin — never a mix — which is what makes the invariant exact.)

Overlap counters (a single call can be counted here *and* in one of the two
origin buckets above — they are NOT part of the reconciliation sum):

- ``tool_calls_malformed``     — tool executions that raised (arg/exec error).
- ``tool_call_unknown_name``   — calls naming a tool that doesn't exist.

Side-channel turn counter (NOT a call count, named explicitly so it can't be
confused with the primaries):

- ``turns_with_text_fallback`` — number of LLM turns on which the text-fallback
  parser fired (≥1 call extracted from prose).
- ``first_text_fallback_iteration`` — 0-based index of the first iteration on
  which the parser extracted ≥1 call from prose; null if it never fired.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Closed vocabulary for ``TaskRecord.task_kind``.
TaskKind = Literal[
    "research",
    "api_spec",
    "code_body",
    "test_authoring",
    "test_run",
    "review",
    "framework_step",
]

#: Closed vocabulary for ``TaskRecord.status``.
TaskStatusLiteral = Literal["passed", "failed", "skipped", "error"]

#: Closed vocabulary for ``TaskRecord.failure_category`` (when non-null).
FailureCategory = Literal["postcondition", "iteration_cap", "tool_error", "model_error"]

#: Log substrings that indicate an asyncio resource leak. ``async_leak_hits``
#: counts lines in the run log matching any of these (grep -c semantics).
ASYNC_LEAK_MARKERS: tuple[str, ...] = (
    "was never awaited",
    "Task was destroyed but it is pending",
    "Event loop is closed",
    "Unclosed client session",
    "Unclosed connector",
)


# ------------------------------------------------------------------ schema models


class ProfileSnapshot(BaseModel):
    """Full inference-config snapshot recorded with each run (NOT just the name)."""

    model_config = {"extra": "forbid"}

    name: str = ""
    model: str
    num_ctx: int | None = None
    max_tokens: int = 0
    temperature: float = 0.0
    seed: int | None = None
    keep_alive: str = ""


class ArmSpec(BaseModel):
    """The experiment arm a run belongs to.

    v1 records both axes; only ``strictness="strict"`` is scored and
    ``scaffolding`` does not yet switch behaviour (see design notes). The
    fields exist so future axes can sweep them without a schema bump.
    """

    model_config = {"extra": "forbid"}

    scaffolding: Literal["lean", "rich"] = "rich"
    strictness: Literal["strict", "permissive"] = "strict"


class PlanEntry(BaseModel):
    """One line of a campaign's ``plan.jsonl`` (written by ``scripts/run_campaign.py``).

    The serialized line carries ``{id, probe, profile, arm, repeat, params}``.
    ``run_id`` is NOT part of that line — it is a load-time enrichment populated
    by :func:`agora.observe.analysis.load_run_records`, which matches this
    entry's per-run subdirectory (named by ``id``) to the ``run.jsonl`` found
    inside it. That lets downstream joins map a run's uuid ``run_id`` back to its
    campaign id and repeat. ``extra="ignore"`` keeps the model forward-compatible
    with future plan keys.
    """

    model_config = {"extra": "ignore"}

    id: str
    probe: str
    profile: str
    arm: ArmSpec = Field(default_factory=ArmSpec)
    repeat: int
    params: dict[str, Any] | None = None
    run_id: str | None = None


class PostconditionOutcome(BaseModel):
    """One ``(name, passed)`` pair from a task's postcondition evaluation."""

    model_config = {"extra": "forbid"}

    name: str
    passed: bool


class TaskRecord(BaseModel):
    """One line in ``tasks.jsonl`` — the outcome of a single task."""

    model_config = {"extra": "forbid"}

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str
    task_id: str
    task_index: int
    role: str
    task_kind: TaskKind
    status: TaskStatusLiteral
    # null (not 0/false) when the field doesn't apply — i.e. status == "skipped":
    # a task that never ran didn't first-pass, didn't loop back zero times, and
    # didn't iterate zero times. Distinguishing "doesn't apply" from "zero
    # happened" keeps campaign aggregations honest.
    first_pass: bool | None
    loopback_count: int | None
    iterations: int | None
    postconditions: list[PostconditionOutcome] = Field(default_factory=list)
    # Primary counters share the "tool call" unit:
    #   tool_calls_structured + tool_calls_text_fallback == tool_calls_total
    tool_calls_total: int = 0
    tool_calls_structured: int = 0
    tool_calls_text_fallback: int = 0
    # Overlap counters (subsets that can also be in an origin bucket above).
    tool_calls_malformed: int = 0
    tool_call_unknown_name: int = 0
    tools_used: list[str] = Field(default_factory=list)
    # Turn-level side channel (NOT a call count).
    turns_with_text_fallback: int = 0
    first_text_fallback_iteration: int | None = None
    failure_category: FailureCategory | None = None
    failure_detail: str | None = None
    duration_s: float = 0.0
    # v3 near-miss capture (findings S4). {path, size_bytes, truncated, text} of
    # the bytes actually written when a task failed with its output present (the
    # gemma equality near-miss). Additive optional; schema_version stays 1.
    artifact_capture: dict[str, Any] | None = None
    # v8 completion-review provenance (S6). ``reviews_used`` is how many times
    # the in-loop read-back fired this task; ``post_review_action`` is what the
    # model did on the turn after the last review. Additive; schema stays 1.
    reviews_used: int = 0
    post_review_action: Literal["confirm", "revise", "other"] | None = None
    # Integration run 1 (v3.2 erratum): completion nudges (S2) that fired. Additive.
    nudges_used: int = 0
    # Integration run 1: per-task phase membership + whether the task gates its
    # phase (verifier tasks are non-blocking). Additive; None phase = pre-run-1.
    phase: str | None = None
    blocking: bool = True
    # Integration run 1: run_check command captures (cmd, exit_code, timed_out,
    # stdout/stderr bounded 4 KB with truncation flags, passed). Additive.
    run_check_records: list[dict[str, Any]] = Field(default_factory=list)
    # F17b (run 2.2): True when this record is a MECHANICAL phase re-eval
    # (cross-phase repair over the workspace), not a live task run — so a later
    # oracle resolves post-repair reality via latest-record-wins. Additive.
    mechanical: bool = False


class RunRecord(BaseModel):
    """The single line in ``run.jsonl`` — the whole-run summary."""

    model_config = {"extra": "forbid"}

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str
    started_at: str
    ended_at: str
    duration_s: float
    probe_name: str
    flow_path: str
    project_name: str
    profile: ProfileSnapshot
    arm: ArmSpec
    success: bool
    exit_code: int
    tasks_total: int
    tasks_passed: int
    tasks_failed: int
    tasks_first_pass: int
    async_leak_hits: int
    model_offloaded: bool | None = None
    tokens_in: int
    tokens_out: int
    ollama_version: str
    git_commit: str
    host: str
    notes: str = ""
    # Per-model prompting strategy (axis-1 v2). Additive optional field:
    # schema_version stays 1 — pre-v2 run.jsonl lines lack the key and parse
    # unchanged (default None = control cell / no strategy).
    strategy: str | None = None
    # v3 harness-reliability config actually in force this run (tool_errors,
    # nudge_budget). Additive optional dict; schema_version stays 1.
    harness: dict[str, Any] | None = None
    # Probe design version (findings S4). Carried from the flow file so v3 cells
    # are never silently compared against v1/v2. Additive optional; stays 1.
    probe_version: int | None = None


class PhaseTaskOutcome(BaseModel):
    """One task's contribution to a phase gate (provenance)."""

    model_config = {"extra": "forbid"}

    task_id: str
    blocking: bool
    passed: bool
    postconditions: list[PostconditionOutcome] = Field(default_factory=list)


class PhaseGateRecord(BaseModel):
    """One line in ``phases.jsonl`` — a phase gate outcome (integration run 1).

    ``passed`` is the gate verdict (all BLOCKING tasks green); ``blockers`` names
    the blocking tasks that failed. Per-task per-predicate outcomes are retained
    so a red gate is diagnosable from provenance alone.
    """

    model_config = {"extra": "forbid"}

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str
    phase: str
    passed: bool
    blockers: list[str] = Field(default_factory=list)
    tasks: list[PhaseTaskOutcome] = Field(default_factory=list)
    # True for a mechanical re-evaluation over the workspace (cross-phase repair)
    # rather than a live task run. Additive; default False. Read as artifact-state.
    mechanical: bool = False


# ------------------------------------------------------------------ pure helpers


def classify_task_kind(
    *,
    output_path: str = "",
    postcondition_names: Sequence[str] = (),
    stage_kinds: Sequence[str] = (),
    role: str = "",
) -> str:
    """Derive a :data:`TaskKind` from a task's structural signals.

    Precedence (first match wins): framework stage kinds → reviewer role →
    api_spec → research (``kb/``) → test authoring (test-file output) → test
    run (pytest/imports postconditions) → ``code_body``.

    Unclassifiable tasks (no output path, no postconditions, no role) fall
    back to ``"code_body"`` with a warning — the contract never invents a new
    vocabulary value.
    """
    pcs = list(postcondition_names)
    op = (output_path or "").replace("\\", "/").lower()
    base = op.rsplit("/", 1)[-1]

    # 1. Any non-LLM/decision stage kind ⇒ a framework mechanical step.
    if any(k not in ("", "llm", "decision") for k in stage_kinds):
        return "framework_step"
    # 2. Reviewer role ⇒ review.
    if role == "reviewer":
        return "review"
    # 3. API-spec authoring.
    if base.endswith("api_spec.md") or any("api_spec" in n for n in pcs):
        return "api_spec"
    # 4. Research / knowledge gathering.
    if op.startswith("kb/") or "/kb/" in op or op.startswith("research/") or "research" in base:
        return "research"
    # 5. Test authoring — the task writes a test file.
    is_test_file = (
        (base.startswith("test_") and base.endswith(".py"))
        or op.startswith("tests/")
        or "/tests/" in op
    )
    if is_test_file or any("has_assertions" in n or "tests_have_assertions" in n for n in pcs):
        return "test_authoring"
    # 6. Test run — verification-only task gated on pytest / import checks.
    if any(n.startswith("pytest") or "py_imports" in n or "python_imports" in n for n in pcs):
        return "test_run"
    # 7. Default.
    if not op and not pcs and not role:
        logger.warning("task_kind unclassifiable (no signals); defaulting to code_body")
    return "code_body"


def derive_status(success: bool, output: str) -> str:
    """Map a task outcome to the :data:`TaskStatusLiteral` vocabulary.

    A task whose ``output`` begins with ``"ERROR: "`` is the orchestrator's
    exception wrapper (the agent loop raised), classified ``"error"``;
    otherwise a non-success is a clean ``"failed"`` (postconditions).
    """
    if success:
        return "passed"
    if (output or "").startswith("ERROR: "):
        return "error"
    return "failed"


def derive_failure(
    *,
    status: str,
    output: str,
    postcondition_results: Sequence[tuple[str, bool, str]],
    tool_calls_malformed: int = 0,
    tool_call_unknown_name: int = 0,
) -> tuple[str | None, str | None]:
    """Classify a non-passing task into ``(failure_category, failure_detail)``.

    ``failure_detail`` is a short token (predicate name / exception head), never
    free-form prose.
    """
    if status in ("passed", "skipped"):
        return None, None
    if status == "error":
        return "model_error", (output or "")[:200].splitlines()[0] if output else "error"
    failed = [(n, reason) for n, passed, reason in postcondition_results if not passed]
    if failed:
        return "postcondition", failed[0][0][:200]
    if tool_call_unknown_name or tool_calls_malformed:
        return "tool_error", "malformed_or_unknown_tool_calls"
    return "postcondition", "unknown"


def scan_async_leaks(log_path: str | Path | None) -> int:
    """Count run-log lines matching any :data:`ASYNC_LEAK_MARKERS` (grep -c).

    Returns 0 when no log path was wired or the file is unreadable.
    """
    if not log_path:
        return 0
    path = Path(log_path)
    if not path.is_file():
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    hits = 0
    for line in text.splitlines():
        if any(marker in line for marker in ASYNC_LEAK_MARKERS):
            hits += 1
    return hits


def git_commit_short(repo_dir: str | Path | None = None) -> str:
    """Best-effort HEAD short SHA; ``"unknown"`` if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_dir) if repo_dir else None,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = out.stdout.strip()
    return sha or "unknown"


def query_ollama_version(base_url: str = "http://localhost:11434") -> str:
    """Best-effort Ollama daemon version via ``/api/version``; ``"unknown"`` on failure."""
    try:
        import urllib.request

        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/version", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data.get("version") or "unknown")
    except Exception:  # noqa: BLE001 — telemetry only, never fail a run
        return "unknown"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ------------------------------------------------------------------ observer


class RunObserver:
    """Accumulates run signals and emits ``run.jsonl`` + ``tasks.jsonl``.

    Construct one per run with the static metadata (profile snapshot, arm,
    probe name, flow path). The orchestrator calls :meth:`task_started` each
    time a task executes (so loop-backs/retries are counted), then
    :meth:`record_task_from_result` per task and :meth:`record_run` once at
    shutdown. Tests can drive the low-level :meth:`record_task` / :meth:`record_run`
    directly.

    The two files are opened (truncated) on construction so a partially
    completed run still leaves valid lines on disk.
    """

    def __init__(
        self,
        *,
        run_id: str,
        output_dir: str | Path,
        probe_name: str,
        flow_path: str,
        project_name: str,
        profile: ProfileSnapshot,
        arm: ArmSpec | None = None,
        log_path: str | Path | None = None,
        ollama_version: str = "unknown",
        git_commit: str = "unknown",
        host: str | None = None,
        notes: str = "",
        strategy: str | None = None,
        harness: dict[str, Any] | None = None,
        probe_version: int | None = None,
    ) -> None:
        self.run_id = run_id
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.probe_name = probe_name
        self.flow_path = flow_path
        self.project_name = project_name
        self.profile = profile
        self.arm = arm or ArmSpec()
        self.log_path = log_path
        self.ollama_version = ollama_version
        self.git_commit = git_commit
        self.host = host or socket.gethostname()
        self.notes = notes
        self.strategy = strategy
        self.harness = harness
        self.probe_version = probe_version
        self.started_at = _utc_now_iso()

        self._run_path = self.output_dir / "run.jsonl"
        self._tasks_path = self.output_dir / "tasks.jsonl"
        self._phases_path = self.output_dir / "phases.jsonl"
        # Truncate on run start; records are appended + flushed as they land.
        self._tasks_fh = self._tasks_path.open("w", encoding="utf-8")
        self._run_fh = self._run_path.open("w", encoding="utf-8")
        # Lazily opened on the first phase-gate record so pre-run-1 flows (no
        # phases) never create an empty phases.jsonl.
        self._phases_fh: Any = None
        self._closed = False

        # Per-task execution counter — drives loopback_count / first_pass.
        self._exec_counts: dict[str, int] = {}

    # -- env-aware construction -------------------------------------------------

    @staticmethod
    def resolve_output_dir(run_id: str, override: str | Path | None = None) -> Path:
        """Resolve the run output dir.

        Precedence: explicit ``override`` → ``AGORA_RUN_OUTPUT_DIR`` env →
        ``runs_out/_default/<run_id>/``.
        """
        import os

        if override is not None:
            return Path(override)
        env = os.getenv("AGORA_RUN_OUTPUT_DIR", "").strip()
        if env:
            return Path(env)
        return Path("runs_out") / "_default" / run_id

    # -- lifecycle hooks --------------------------------------------------------

    def task_started(self, task_id: str) -> None:
        """Record that ``task_id`` began an execution (counts retries/loop-backs)."""
        self._exec_counts[task_id] = self._exec_counts.get(task_id, 0) + 1

    def exec_count(self, task_id: str) -> int:
        """How many times ``task_id`` executed (0 if never)."""
        return self._exec_counts.get(task_id, 0)

    # -- low-level emit (test-drivable) -----------------------------------------

    def record_task(self, **fields: Any) -> TaskRecord:
        """Validate + append one :class:`TaskRecord` to ``tasks.jsonl``."""
        fields.setdefault("run_id", self.run_id)
        record = TaskRecord(**fields)
        self._write(self._tasks_fh, record)
        return record

    def record_phase_gate(
        self, result: Any, *, run_id: str | None = None
    ) -> PhaseGateRecord:
        """Append one phase-gate outcome to ``phases.jsonl`` (integration run 1).

        Accepts a :class:`agora.fleet.phase_gate.PhaseGateResult` (duck-typed:
        ``phase``, ``passed``, ``blockers``, and ``tasks`` of
        ``TaskGateOutcome``). Opens ``phases.jsonl`` on first call.
        """
        record = PhaseGateRecord(
            run_id=run_id or self.run_id,
            phase=result.phase,
            passed=bool(result.passed),
            blockers=list(result.blockers),
            tasks=[
                PhaseTaskOutcome(
                    task_id=t.task_id,
                    blocking=t.blocking,
                    passed=t.passed,
                    postconditions=[
                        {"name": n, "passed": bool(p)} for n, p in t.postconditions
                    ],
                )
                for t in result.tasks
            ],
        )
        if self._phases_fh is None:
            self._phases_fh = self._phases_path.open("w", encoding="utf-8")
        self._write(self._phases_fh, record)
        return record

    def record_run(self, **fields: Any) -> RunRecord:
        """Validate + write the single :class:`RunRecord` to ``run.jsonl``.

        Computes ``async_leak_hits`` from the wired log path when not supplied.
        """
        fields.setdefault("run_id", self.run_id)
        fields.setdefault("started_at", self.started_at)
        fields.setdefault("ended_at", _utc_now_iso())
        fields.setdefault("probe_name", self.probe_name)
        fields.setdefault("flow_path", self.flow_path)
        fields.setdefault("project_name", self.project_name)
        fields.setdefault("profile", self.profile)
        fields.setdefault("arm", self.arm)
        fields.setdefault("ollama_version", self.ollama_version)
        fields.setdefault("git_commit", self.git_commit)
        fields.setdefault("host", self.host)
        fields.setdefault("notes", self.notes)
        fields.setdefault("strategy", self.strategy)
        fields.setdefault("harness", self.harness)
        fields.setdefault("probe_version", self.probe_version)
        if "async_leak_hits" not in fields:
            fields["async_leak_hits"] = scan_async_leaks(self.log_path)
        record = RunRecord(**fields)
        self._write(self._run_fh, record)
        return record

    # -- high-level emit (orchestrator-facing) ----------------------------------

    def record_task_from_result(
        self,
        *,
        task: Any,
        result: Any | None,
        role: str,
        task_index: int,
    ) -> TaskRecord:
        """Build + emit a :class:`TaskRecord` from a task and its ``TaskResult``.

        ``result`` is ``None`` for tasks that never executed (``skipped``).
        """
        task_id = getattr(task, "id", "")
        output_path = getattr(task, "output_path", "") or ""
        spec = getattr(task, "spec", None)
        pc_names = [getattr(p, "name", "") for p in getattr(spec, "postconditions", ())]
        stage_kinds: list[str] = []
        for st in getattr(task, "stages", ()) or ():
            stage_kinds.append(getattr(st, "kind", "llm"))

        kind = classify_task_kind(
            output_path=output_path,
            postcondition_names=pc_names,
            stage_kinds=stage_kinds,
            role=role,
        )
        execs = self.exec_count(task_id)

        if result is None:
            # Skipped: the task never ran. The three run-shape fields don't
            # apply, so emit null rather than a misleading zero/false.
            return self.record_task(
                task_id=task_id,
                task_index=task_index,
                role=role,
                task_kind=kind,
                status="skipped",
                first_pass=None,
                loopback_count=None,
                iterations=None,
                postconditions=[],
                duration_s=0.0,
                phase=(getattr(task, "phase", "") or None),
                blocking=bool(getattr(task, "blocking", True)),
            )

        success = bool(getattr(result, "success", False))
        output = getattr(result, "output", "") or ""
        pc_results = list(getattr(result, "postcondition_results", []) or [])
        status = derive_status(success, output)
        malformed = int(getattr(result, "tool_calls_malformed", 0) or 0)
        unknown = int(getattr(result, "tool_call_unknown_name", 0) or 0)
        failure_category, failure_detail = derive_failure(
            status=status,
            output=output,
            postcondition_results=pc_results,
            tool_calls_malformed=malformed,
            tool_call_unknown_name=unknown,
        )
        loopback_count = max(0, execs - 1)
        first_pass = success and execs <= 1

        return self.record_task(
            task_id=task_id,
            task_index=task_index,
            role=role,
            task_kind=kind,
            status=status,
            first_pass=first_pass,
            loopback_count=loopback_count,
            iterations=int(getattr(result, "iterations", 0) or 0),
            postconditions=[
                {"name": n, "passed": bool(p)} for n, p, _reason in pc_results
            ],
            tool_calls_total=int(getattr(result, "tool_calls_total", 0) or 0),
            tool_calls_structured=int(getattr(result, "tool_calls_structured", 0) or 0),
            tool_calls_text_fallback=int(getattr(result, "tool_calls_text_fallback", 0) or 0),
            tool_calls_malformed=malformed,
            tool_call_unknown_name=unknown,
            tools_used=sorted(set(getattr(result, "tools_used", []) or [])),
            turns_with_text_fallback=int(
                getattr(result, "turns_with_text_fallback", 0) or 0
            ),
            first_text_fallback_iteration=getattr(
                result, "first_text_fallback_iteration", None
            ),
            failure_category=failure_category,
            failure_detail=failure_detail,
            duration_s=float(getattr(result, "duration_s", 0.0) or 0.0),
            artifact_capture=getattr(result, "artifact_capture", None),
            reviews_used=int(getattr(result, "reviews_used", 0) or 0),
            post_review_action=getattr(result, "post_review_action", None),
            nudges_used=int(getattr(result, "nudges_used", 0) or 0),
            phase=(getattr(task, "phase", "") or None),
            blocking=bool(getattr(task, "blocking", True)),
            run_check_records=list(getattr(result, "run_check_records", []) or []),
        )

    def close(self) -> None:
        """Flush + close both file handles. Idempotent."""
        if self._closed:
            return
        for fh in (self._tasks_fh, self._run_fh, self._phases_fh):
            if fh is None:
                continue
            try:
                fh.flush()
                fh.close()
            except OSError:
                pass
        self._closed = True

    # -- internals --------------------------------------------------------------

    @staticmethod
    def _write(fh: Any, record: BaseModel) -> None:
        fh.write(record.model_dump_json() + "\n")
        fh.flush()


def profile_snapshot_from(profile: Any) -> ProfileSnapshot:
    """Build a :class:`ProfileSnapshot` from a :class:`ModelProfile`.

    ``temperature`` and ``seed`` are read from the profile itself — the same
    values :func:`agora.fleet.profiles.build_llm_factory` threads into the
    Ollama options dict — so the recorded snapshot is a true record of what
    the daemon was told, not a caller-supplied annotation.
    """
    seed = getattr(profile, "seed", None)
    return ProfileSnapshot(
        name=getattr(profile, "name", "") or "",
        model=getattr(profile, "model", ""),
        num_ctx=getattr(profile, "num_ctx", None),
        max_tokens=int(getattr(profile, "max_tokens", 0) or 0),
        temperature=float(getattr(profile, "temperature", 0.0) or 0.0),
        seed=None if seed is None else int(seed),
        keep_alive=getattr(profile, "keep_alive", "") or "",
    )


__all__ = [
    "ArmSpec",
    "PlanEntry",
    "PostconditionOutcome",
    "ProfileSnapshot",
    "RunObserver",
    "RunRecord",
    "SCHEMA_VERSION",
    "TaskRecord",
    "classify_task_kind",
    "derive_failure",
    "derive_status",
    "git_commit_short",
    "profile_snapshot_from",
    "query_ollama_version",
    "scan_async_leaks",
]
