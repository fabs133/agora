# Axis 1 findings — tool-call fidelity

*Campaign completed 2026-07-01.
Evidence: `docs/runs/axis-1-tool-call-fidelity/`.
Analysis code: `src/agora/observe/analysis.py`,
`src/agora/observe/layer2.py`, `scripts/analyze_layer2.py`.
Capability vectors: regenerable via `python scripts/analyze_layer2.py`.*

## Purpose

Agora is designed to compose multiple models across specialized roles.
Doing that responsibly requires empirical evidence about each
candidate model's capabilities on the axes that matter for
orchestration — not architecture-level assumptions, not README
folklore, not a single reference-run outcome.

Axis 1 measures **tool-call fidelity**: can a model emit
well-formed structured tool calls, in the right order, with the right
arguments, adapting to what tool results actually return? Tool-call
fidelity is the gating capability for the implementer and tester
roles. If it fails, no other downstream ability matters — the
orchestrator's loop never advances.

## Method

**Probe.** `flows/tool-call-fidelity.plan.yaml`. One implementer
agent, three tasks, deliberately code-free so failures attribute
solely to tool-call behaviour:

- `small_chain` (max_iter 5): read a seed file, write its contents
  verbatim to an output path, mark complete. Three tool calls total.
- `loop_depth` (max_iter 12): list a directory, read two files,
  write their concatenation, mark complete. Five tool calls total,
  higher iteration ceiling.
- `content_robustness` (max_iter 4): read a redirect file whose
  content names another file to read; write that second file's bytes
  and mark complete. Tests whether the model adapts to
  non-trivial tool-result content.

All artifacts the model must produce are given verbatim in the
instructions. Auto-hooks are disabled (`enable_auto_hooks=False`) —
this is the atomic-probe principle: `mark_complete_called` reflects
the model's own call, and tool-call counts are purely
model-emitted, without framework synthesis.

**Sweep.** `campaigns/axis-1-tool-call-fidelity.yaml`. Six models:

- qwen2.5-coder:7b
- qwen2.5-coder:14b
- qwen2.5:7b-instruct
- gemma4:e4b (~8B active params)
- mistral-nemo:12b-instruct
- qwen3:30b (MoE, A3B active)

Two arms (`lean`, `rich`) × three repeats = **6 runs per model, 36 runs total**.
The scaffolding-lean/scaffolding-rich distinction is deferred in v1
(both arms currently route through the scaffolded path); the arm
dimension is preserved for future measurement and produces
repeatability data now.

**Params locked across the sweep**: `temperature=0.0`, `seed=42`,
`num_ctx=8192`, `max_tokens=2048`. Ollama 0.24, Windows 11, Tesla P40
(24 GiB, pinned via `CUDA_VISIBLE_DEVICES` bus ID).

**Data contract.** Each run emits `run.jsonl` (one record) and
`tasks.jsonl` (one record per task). Schema is versioned. Tool-call
counts obey the invariant `structured + text_fallback == total` — the
Correction 2 unit reconciliation ensures every field is measured in
the same units. Reproducibility is captured in every run's profile
snapshot.

## Results

### Per-model behaviour

Aggregating over all 6 runs per model:

| Model | Pass rate | Structured calls | Text-fallback calls | Behavioural class |
|---|---:|---:|---:|---|
| gemma4:e4b | 12 / 18 | 72 | 0 | structured-succeeds |
| qwen3:30b | 3 / 18 | 33 | 0 | structured-fragile |
| qwen2.5:7b-instruct | 2 / 18 | 51 | 0 | structured-fragile |
| mistral-nemo:12b | 0 / 18 | 18 | 18 | mixed-fails |
| qwen2.5-coder:7b | 0 / 18 | 0 | 12 | narrate-fallback |
| qwen2.5-coder:14b | 0 / 18 | 0 | 12 | narrate-fallback |

### Per-task, showing which tasks discriminate

Passes out of 6 repeats each:

| Model | small_chain | loop_depth | content_robustness |
|---|:---:|:---:|:---:|
| gemma4:e4b | 6 / 6 | 0 / 6 | 6 / 6 |
| qwen3:30b | 3 / 6 | 0 / 6 | 0 / 6 |
| qwen2.5:7b-instruct | 1 / 6 | 0 / 6 | 1 / 6 |
| mistral-nemo:12b | 0 / 6 | 0 / 6 | 0 / 6 |
| qwen2.5-coder:7b | 0 / 6 | 0 / 6 | 0 / 6 |
| qwen2.5-coder:14b | 0 / 6 | 0 / 6 | 0 / 6 |

### Reproducibility (steady-state, excluding block-first)

Under `temperature=0, seed=42`, repeats within a `(model, arm)` cell
are expected to produce byte-identical JSONL. Measured:

| Model | All-6 reproducibility | Steady-state | Note |
|---|:---:|:---:|---|
| gemma4:e4b | 1.00 | 1.00 | fully deterministic |
| mistral-nemo:12b | 1.00 | 1.00 | fully deterministic (in failure) |
| qwen2.5-coder:7b | 1.00 | 1.00 | fully deterministic (in narration) |
| qwen2.5-coder:14b | 1.00 | 1.00 | fully deterministic (in narration) |
| qwen2.5:7b-instruct | 0.00 | 1.00 | block-first differs; steady-state clean |
| qwen3:30b | 0.67 | 0.67 | non-deterministic in steady state |

Steady-state excludes one prewarm-contaminated run per block for
qwen2.5:7b-instruct (see caveats).

## Findings

### 1. The probe discriminates cleanly on behavioural class

Six models separate into four distinct behavioural classes. The
class distinctions are qualitatively larger than any within-class
variation the probe measures — this is a genuine capability
partition, not a scoring artifact.

- **structured-succeeds** (gemma4:e4b) — emits well-formed
  structured tool calls, completes the tasks it attempts.
- **structured-fragile** (qwen3:30b, qwen2.5:7b-instruct) — emits
  structured tool calls when it emits them, but frequently bails
  without emitting anything on turn 1.
- **narrate-fallback** (qwen2.5-coder:7b, qwen2.5-coder:14b) — emits
  tool calls as JSON in prose rather than in the structured `tool_calls`
  field, if at all; the text-fallback parser extracts them, but the
  loop typically breaks before task completion.
- **mixed-fails** (mistral-nemo:12b) — split roughly evenly between
  structured and text-fallback emission across tasks, completes none.

Between-model variance exceeds within-repeat variance across all
observable dimensions in the deterministic subset (1.65 vs 0.94 on
structured emission rate). Once non-deterministic models are
included, the count metrics muddy (1.38 vs 1.46), but the
behavioural class boundaries remain robust because they're
categorical, not quantitative.

### 2. Coder variants tool-call worse than their instruct counterparts,
### and size does not help — the cause is a training-regime difference

qwen2.5-coder:7b and qwen2.5-coder:14b produce byte-identical outputs
on this probe: zero structured tool calls, twelve text-fallback
extractions each, zero task passes. The 2× parameter jump changes
nothing about their tool-call behaviour. Both narrate prose describing
what they intend to do rather than emitting the structured tool_calls
field.

The mechanistic cause is documented externally: **Qwen2.5-Coder was
not trained on tool-call tokens**, unlike Qwen2.5-Instruct which was
trained with Hermes-style tool-call markers (`<tool_call>...</tool_call>`).
The Ollama Modelfile for qwen2.5-coder attempts to induce tool-call
output via prompt template, but since the underlying model has no
trained representation for the format, the induction is fragile —
the model preferentially emits ```json``` code blocks (which our
text-fallback parser correctly extracts) rather than native structured
calls. This is why scaling from 7B to 14B does not help: parameter
count cannot substitute for absent training-regime capability.

qwen2.5:7b-instruct, in contrast, tool-calls structured, always,
with zero text-fallback across all 18 tasks. Same parameter count,
same model family, radically different behaviour. Instruct-tuning —
specifically the inclusion of tool-call tokens in the training data —
matters more than size for this axis.

This finding transfers beyond Agora: the vLLM community independently
documented the same behaviour and built a coder-specific parser plus
few-shot template to work around it (see References). Agora's
text-fallback parser is performing analogous compensation. The finding
is a property of the model family, not the framework.

### 3. gemma4:e4b is the strongest tool-caller in the candidate set

At ~8B active parameters, gemma4:e4b passes 12 of 18 tasks (67%),
the highest of any model, with fully deterministic behaviour and
zero text-fallback across all runs. It solves `small_chain` and
`content_robustness` at 6/6 each; only `loop_depth` defeats it
(see finding 5).

This is a substantive finding for role-fit routing: on tool-call
fidelity, the model with the fewest active parameters is the best
candidate. Size is not the dominant lever; training regime is.

### 4. qwen3:30b passes the `<think>`-stripping test, but exposes a
### malformed tool-call quirk and steady-state non-determinism

qwen3:30b is a reasoning model that emits `<think>...</think>`
sections. The OllamaAdapter's stripping (landed on
`research/campaign-axis-1`) is a prerequisite for measuring anything:
without it, reasoning content contaminates the assistant message and
the tool-call parser sees noise.

Across all 18 qwen3:30b tasks, `tool_calls_text_fallback = 0` and
`turns_with_text_fallback = 0` — stripping works end-to-end, no
reasoning-block leakage confused the parsers.

Two anomalies specific to qwen3:30b:

- **Malformed tool calls, only on this model.** On the passing
  `small_chain` trajectory, qwen3:30b emits 3 malformed calls
  alongside 7 structured ones. All 9 malformed calls across the sweep
  are identical: `mark_complete` invoked with `{content, path}`
  (a file-writing tool's argument schema) instead of its real
  `{artifacts, summary}` schema, producing `KeyError: 'summary'`. The
  model recovers on the 4th attempt, which is why those runs still
  pass. This is a capability quirk (tool-schema confusion under the
  reasoning-model regime), not a validator over-strictness.
- **Steady-state non-determinism.** On `small_chain`, qwen3:30b
  alternates between exactly two byte-reproducible trajectories:
  7 calls → pass, or 0 calls → fail. This survives excluding the
  prewarm-contaminated block-first run (see caveats), so it is not an
  infrastructure artifact. The likely mechanism is MoE routing plus
  loaded-model state carryover across consecutive runs. Reproducibility
  therefore reads 0.67, and the qualifier is real: **qwen3:30b's
  tool-call behaviour is not fully deterministic under nominally
  deterministic conditions**.

### 5. `loop_depth` is unsolved by every model in the sweep

Zero passes across 36 runs (6 models × 6 repeats). Every model that
attempted `loop_depth` bailed after one iteration with zero tool
calls, or aborted early with an incomplete chain.

The probe's difficulty distribution is therefore uneven: `small_chain`
is near-binary (models solve it or they don't emit at all),
`content_robustness` discriminates the middle band, and `loop_depth`
is a floor no candidate reaches. `loop_depth` produces one bit of
information ("no model in the set clears this bar"), and the axis-1
signal comes almost entirely from the other two tasks.

For future probes, this is a **methodology lesson**: aim for tasks
whose pass-rate spans roughly 20–80% across the candidate set. A
task at 0% is measuring difficulty as a constant rather than as a
variable. This does not invalidate axis-1 v1's data — the other two
tasks discriminate cleanly — but the loop-depth threshold identified
here informs where the "next hardest tier" should sit when we design
axis-2 probes.

## Caveats

Three qualifications on the numbers above, in order of how much they
affect interpretation.

**Prewarm num_ctx bug.** The campaign harness's model prewarm
step (during model eviction) did not forward the profile's
`num_ctx`. First runs after eviction therefore loaded the model at
Ollama's default context (32768) rather than the profile-pinned
value (8192). This produced one contaminated run per model block —
the "block-first" run. Evidence: every block-first shows
`CONTEXT=32768` in the pre-run snapshot, every subsequent steady-
state run shows 8192.

The contamination is fully explanatory for qwen2.5:7b-instruct's
all-6 reproducibility reading of 0.00 (r013 diverges from
r014–r018; steady-state across the five clean runs is 1.00). For
qwen3:30b, the contamination is one contributing factor but does
not fully explain the non-determinism — steady-state across five
runs is still 0.67. Two independent mechanisms coexist for that
model.

Fixed on `fix/prewarm-num-ctx` (commit 55de631). Future campaigns
will not carry this contamination. The `excluded_repeats` column
in `capability_vectors.csv` records where corrections were applied
per cell.

**Provisional normalization.** `normalized_score` in the capability
vector CSV is identity for rates that are naturally 0–1 (higher is
better) and null elsewhere (text_fallback and malformed rates,
which are lower-is-better; iteration medians, which are on a
different scale). A proper normalization scheme awaits a second
axis's data — with only one axis measured, we can't yet distinguish
"low score because low capability" from "low score because the axis
happens to have a compressed range." Cell values are usable as-is
for within-axis comparison; cross-axis composition should wait.

**Loop-depth signal is a null result.** Zero-pass rates convey
"no model clears this bar" but nothing about the bar's position
relative to the models' true capability distribution. Treating this
as "loop_depth is impossible for these models" would overreach; it
could equally be "loop_depth is one unit past what any of them can
do." A future probe with a graduated `loop_depth` (3, 4, 5, 7 tools)
would locate each model's actual ceiling.

## Capability vectors

Regeneration: `python scripts/analyze_layer2.py`
(output: `runs_out/axis-1-tool-call-fidelity/reports/capability_vectors.csv`).

Schema:
`model, axis, sub_target, raw_value, normalized_score, repeats, excluded_repeats, ci_low, ci_high`

Axis: `tool_call_fidelity`. Seven sub_targets per model:

- `structured_emission_rate` — structured calls / total calls
- `pass_rate` — tasks passed / tasks attempted
- `text_fallback_rate` — text-fallback calls / total calls
- `malformed_call_rate` — malformed calls / total calls
- `content_adaptation_rate` — pass rate on `content_robustness`
  (adapting to redirected tool-result content)
- `first_fallback_iteration_p50` — median iteration where
  fallback first fired; null if never
- `trajectory_reproducibility_rate` — fraction of same-input runs
  producing identical output (steady-state where the block-first
  was excluded)

For axis-1 v1 the CSV has 42 rows (6 models × 7 sub_targets).

## What this axis does not measure

Explicit boundary: axis 1 measures tool-call fidelity under
conditions where the code the model must produce is given
verbatim. It does not measure code correctness (axis 3), instruction
adherence to output-format constraints (axis 2), plan authoring
(axis 4), cross-file consistency (axis 5), or failure-recovery
(axis 6). A model can be a strong tool-caller and a poor code
author, or vice versa. Downstream role-fit routing should combine
scores across axes, not treat axis 1 as a sufficient signal for
implementer-role selection.

The measurement is also scoped to Agora's current tool-call
protocol: standard Ollama-format tool manifests, generic tool
descriptions, no per-model prompting strategy. Some findings (notably
finding 2) may respond to per-model formatting adjustments; see Open
questions.

## Provisional implications for role-fit

These are pending measurement on axes 2–6 and should be re-evaluated
after each subsequent characterization campaign.

- **Implementer / tester roles** (tool-call fidelity is a gating
  requirement): gemma4:e4b is the leading candidate on this axis.
  qwen2.5:7b-instruct is viable when reliability requirements
  can tolerate the "structured-fragile" class. qwen3:30b is not
  yet routable given the steady-state non-determinism; if a future
  fix addresses that, it becomes a strong candidate. The
  qwen2.5-coder variants are not viable under the current tool-call
  protocol regardless of size — see Open questions for the
  formatting-strategy path that could change this.
- **Architect role** (structured authoring axis 4, not yet measured):
  no conclusions from axis 1; architect selection awaits its own
  campaign.

## Open questions

Three hypotheses raised by axis-1 data that were not tested in v1,
documented here so they inform future work rather than being
rediscovered:

1. **Do coder variants respond to few-shot induction?** The vLLM
   community's coder-specific parser achieved 100% pass rates on 7B/32B
   with template-injected few-shot examples (see References). Whether
   the same technique lifts qwen2.5-coder:7b/14b's Agora pass rates
   from 0/18 to something meaningfully non-zero is an untested
   hypothesis with strong external evidence supporting it.
2. **Is mistral-nemo:12b's mixed emission format-related or
   capability-limited?** The model splits evenly between structured
   and text-fallback but completes no tasks. This could be a
   tool-format mismatch (the model expects a different manifest
   shape than Ollama's Hermes-style default) or an actual
   task-completion weakness. The current data cannot distinguish.
3. **Does qwen3:30b's steady-state non-determinism respond to
   prompting changes?** The MoE routing hypothesis suggests it might
   be intrinsic; a prompting variation might swamp or expose it. The
   answer bears on whether the model can be routed at all.

The natural experiment to address 1 and 2 is a per-model prompting
strategy layer (analogous to vLLM's `--tool-call-parser` and
`--chat-template` flags). Design consideration: such a layer should
be orthogonal to `ModelProfile` — prompting strategy is a policy
over how work is presented, not a property of the model + hardware
config. If this becomes a build target, the right shape is a
separate `PromptingProfile` selected alongside `ModelProfile` at
run construction.

Whether to build it is a decision informed by whether axes 2–6
surface similar format-related patterns. If they do, the resolver
layer becomes justified by multiple observed failures across axes,
which satisfies the framework's evidence-first principle. If they
don't, axis 1's coder finding remains valid on its own terms and
the layer can wait indefinitely.

## Next steps

- **`fix/prewarm-num-ctx` merges to `main`** so subsequent campaigns
  do not carry the contamination. No axis-1 rerun is planned; the
  `excluded_repeats` mechanism captures the correction in the
  existing data.
- **Axis 2 probe design** (instruction adherence). Methodology
  lessons from axis 1 to apply: task pass-rates should span roughly
  20–80% across the candidate set, not bottom out at 0%.
- **Optional targeted rerun** of qwen3:30b's six cells post-fix if
  its steady-state bistability becomes decision-relevant. Not
  currently planned.

## References

External technical evidence supporting the interpretations above:

- Qwen team official documentation on Qwen2.5 function calling —
  https://qwen.readthedocs.io/en/latest/framework/function_call.html
  (documents Hermes-style tool calling as the Qwen2.5 family default;
  describes training-regime inclusion in instruct variants)
- vLLM Issue #32926: dedicated Qwen2.5-Coder tool parser —
  https://github.com/vllm-project/vllm/issues/32926
  (documents that Qwen2.5-Coder was not trained on tool-call tokens
  and outputs ```json``` code blocks instead of structured calls;
  proposes coder-specific parser)
- Hermes-agent Issue #5867 —
  https://github.com/NousResearch/hermes-agent/issues/5867
  (independent confirmation of the same behaviour in Ollama context:
  qwen2.5-coder returns tool calls as JSON strings in the content
  field rather than the tool_calls array)
- Ollama qwen2.5-coder template —
  https://www.ollama.com/library/qwen2.5-coder/blobs/e94a8ecb9327
  (shows the Modelfile template that attempts to induce
  `<tool_call>` format; useful for understanding the delivery
  mechanism)

---

## Integrity note (2026-07-03, retroactive)

Post-v3 forensics (docs/runs/axis-1-v3/forensics-stale-out.md) found that
workspace out/ was never reset between runs and write_file's overwrite
guard blocked live writes: of this campaign's recorded passes, 2 were live
and 46 were stale-file artifacts. Task-success attributions in this
document are superseded by the v2 findings' Integrity addendum (§8);
trajectory-level findings (emission channels, call volumes, determinism,
behavioural classes) are unaffected and stand.
