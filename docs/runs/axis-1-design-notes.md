# Axis-1 design notes — tool-call fidelity (Phase A, locked)

Durable record of the Phase A foundation: the structured-logging schema, the
tool-call fidelity probe, and the campaign sweep. Everything here is **locked** —
later axes extend it, they do not redesign it.

## 1. JSONL schema v1 (locked)

Every run that wires a `RunObserver` (`agora.observe.jsonl`) emits two files into
a per-run output directory (`AGORA_RUN_OUTPUT_DIR`, default
`runs_out/_default/<run_id>/`):

- `run.jsonl` — exactly one `RunRecord` line (whole-run summary).
- `tasks.jsonl` — one `TaskRecord` line per task, in execution order.

Both carry `schema_version: 1`. The pydantic models in `agora.observe.jsonl` are
the source of truth; closed vocabularies are enforced via `Literal`.

### RunRecord
`schema_version, run_id, started_at, ended_at, duration_s, probe_name, flow_path,
project_name, profile{name,model,num_ctx,max_tokens,temperature,seed,keep_alive},
arm{scaffolding,strictness}, success, exit_code, tasks_total, tasks_passed,
tasks_failed, tasks_first_pass, async_leak_hits, model_offloaded, tokens_in,
tokens_out, ollama_version, git_commit, host, notes`.

`profile` is a **full snapshot**, not just the name — and `temperature`/`seed`
are the values the Ollama options dict actually received (threaded through
`build_llm_factory`), so the record is a true reproducibility contract, not a
hopeful annotation.

### TaskRecord
`schema_version, run_id, task_id (TaskTemplate id, not the instantiated UUID),
task_index, role, task_kind, status, first_pass, loopback_count, iterations,
postconditions[{name,passed}], tool_calls_total, tool_calls_structured,
tool_calls_text_fallback, tool_calls_malformed, tool_call_unknown_name,
tools_used[], turns_with_text_fallback, first_text_fallback_iteration,
failure_category, failure_detail, duration_s`.

Closed vocabularies:
- `task_kind`: research, api_spec, code_body, test_authoring, test_run, review,
  framework_step (derived from output_path / postconditions / stage kinds;
  unclassifiable → code_body + warning, never a new value).
- `status`: passed, failed, skipped, error.
- `failure_category` (nullable): postcondition, iteration_cap, tool_error,
  model_error.

**Null-vs-zero (Checkpoint-1 fix):** for `status == "skipped"`, `first_pass`,
`loopback_count`, and `iterations` are **null** — the task never ran, so those
fields do not apply. Aggregations must not conflate "doesn't apply" with "zero
happened" (a typical failed run skips ~8 of 12 tasks).

### Tool-call accounting (units reconciled)
The three primary counters share the **tool call** unit and reconcile exactly:

    tool_calls_structured + tool_calls_text_fallback == tool_calls_total

The Ollama adapter's text fallback (`_parse_tool_calls_from_text`) only runs when
the native `tool_calls` field was empty, so a given turn's calls are wholly one
origin — never a mix — which is what makes the invariant exact.
`tool_calls_malformed` / `tool_call_unknown_name` are *overlap* counters (a call
counted there is also in one origin bucket; they are not part of the sum).
`turns_with_text_fallback` is a turn-level side channel (NOT a call count), and
`first_text_fallback_iteration` is the 0-based index of the first iteration the
parser extracted ≥1 call from prose (null if never).

### Fail-closed postconditions
Every content-reading predicate returns `False` on a missing/empty file — never a
vacuous pass — so postcondition pass-rate stays a clean signal. Audited and
regression-tested across `predicate_registry` and `runtime_postconditions`.

## 2. The probe (`flows/tool-call-fidelity.plan.yaml`)

One implementer agent (empty `model:` → profile-driven), three single-stage
tasks. Each task dictates the exact tool sequence and brief, byte-exact,
machine-checkable postconditions.

- `small_chain` (max_iterations 5): `read_file(plan/seed.txt)` →
  `write_file(out/seed_copy.txt, verbatim)` → `mark_complete`.
- `loop_depth` (max_iterations 12): `list_directory(plan)` →
  `read_file(seed_a)` → `read_file(seed_b)` →
  `write_file(out/concat.txt, A then B, no separator)` → `mark_complete`.
- `content_robustness` (max_iterations 4): `read_file(plan/redirect.txt)` → read
  the file its content names → `write_file(out/final.txt, verbatim)` →
  `mark_complete`. If the redirect target can't be determined, `mark_complete`
  saying so.

Predicates (new, in `agora.plan.probe_predicates`, registered into the registry):
`file_content_equals_seed`, `file_content_equals_concat`, `mark_complete_called`
— all byte-exact and fail-closed. (`file_exists` from the registry backs the
"file at expected path" check.)

The runner `scripts/run_tool_call_fidelity.py` seeds the four `plan/*.txt`
fixtures, then runs the probe through `build_orchestrator` with the observer on.

### Design decisions
- **A — atomic probe.** No code generation, no decomposition judgment. Every
  step is dictated, so a failure attributes to the tool-call axis (well-formed
  calls, correct order, verbatim bytes, mark_complete) and nothing else. This is
  the whole point: a clean per-model fidelity number, not a confounded one.
- **B — auto-hooks OFF for the probe.** With auto-hooks on, the framework
  synthesizes `mark_complete` from written files and auto-commits. That would
  make `mark_complete_called` pass trivially and inject non-model tool calls.
  The runner sets `auto_hooks_enabled=False` so the signal is purely the model's
  own calls.
- **C — byte-exact, fail-closed postconditions.** `read_bytes` equality (not
  normalized text) catches reformatting / whitespace drift, and missing files
  fail closed — both keep pass-rate meaningful.
- **D — staged single-task wrapping for per-task iteration caps.** Each task is
  one stage so `max_iterations` (5/12/4) is expressible in YAML and the chains
  get tight, model-appropriate budgets without bespoke runner code.

## 3. Campaign sweep (`campaigns/axis-1-tool-call-fidelity.yaml`)

EXPLICIT-form sweep, 36 runs = 1 probe × 6 models × 2 arms × 3 repeats, ids
`r001`..`r036`. Ordered to **minimize model swaps**: all three repeats of one
`(model, arm)` are consecutive, then the arm flips, then the model changes — so
each model loads at most twice across its six runs.

Models, in order:
`qwen-coder-7b, qwen-coder-14b, qwen-instruct-7b, gemma-e4b, mistral-nemo-12b,
qwen3-30b`. `qwen-coder-32b` is **deliberately omitted** (VRAM gate — it needs
num_ctx=4096 to fit a single P40 and the campaign pins num_ctx=8192).

`defaults.params`: `{temperature: 0.0, seed: 42, num_ctx: 8192, max_tokens:
2048}`; `output_dir: runs_out/axis-1-tool-call-fidelity`; `resume: true`. Per-run
override of any default is allowed (the reason for explicit form). The committed
YAML is the output of `scripts/expand_campaign.py` — re-runnable after a profile
rename without hand-editing 36 lines.

### Arm semantics in v1 (important)
`arm.strictness` and `arm.scaffolding` are **recorded but not yet behavioural**.
v1 has a single scoring mode (strict) and a single scaffolded execution path, so
**`arm=lean` and `arm=rich` produce identical runs** in this campaign. The two
arms are still worth sweeping: three repeats × two arms = six samples per model,
which is repeatability/variance data even before the lean-vs-rich machinery
lands in a later branch. The fields exist so future axes sweep them without a
schema bump.
