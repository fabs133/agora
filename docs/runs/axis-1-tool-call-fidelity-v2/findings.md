# Axis 1 v2 findings — per-model prompting strategies

*Written 2026-07-03 from the pre-committed skeleton (see git history of
this file: table shells, verdict boxes, and reading rules were fixed
before campaign execution; this revision fills slots). Companions:
`docs/runs/axis-1-findings.md` (v1), `docs/research/model-roster.md`
(Phase 1), `docs/research/prompting-strategies.md` (Phase 2
pre-registration — thresholds quoted verbatim).*

*Campaign: `campaigns/axis-1-tool-call-fidelity-v2.yaml` — 40 runs, A/B
on the `strategy` field. Evidence: `runs_out/axis-1-tool-call-fidelity-v2/`.
Daemon: 0.31.1 uniform. git_commit: 5e5368b (frozen merge of
feat/axis-1-strategies). Executed: 2026-07-03. All numbers below were
independently regenerated from raw JSONL via `analyze_layer2.py` /
`agora.observe.analysis.load_campaign` and cross-checked against the
executor's per-block reports — full agreement.*

---

## 0. Data-integrity gate

| Check | Expected | Observed | Pass |
|---|---|---|---|
| Runs complete | 40/40 run.jsonl present + parse | 40/40, strict pydantic parse clean | ☑ |
| Call-count invariant | structured + text_fallback == total, every task record | holds on all 120 task records | ☑ |
| Daemon version uniform | ollama_version identical across all 40 | {0.31.1} | ☑ |
| Context pinned | every snapshot_pre CONTEXT = 8192, incl. block-firsts | {8192}; prewarm fix live; `excluded_repeats` = 0 everywhere (v1: block-firsts excluded) | ☑ |
| Strategy provenance | RunRecord.strategy matches YAML per run id | 31 null / 6 qwen2_5_coder / 3 mistral_nemo, per-run match | ☑ |
| git_commit uniform | all 40 identical, equals frozen merge commit | {5e5368b} | ☑ |

**One incident, explained and scoped (see Caveat C1):** the campaign
root `plan.jsonl` was found truncated to the final block (10/40 lines) —
an infrastructure bug in staged execution (`run_campaign.py` writes
plan.jsonl with truncating `write_text`; the staged sweep invokes it
per block). Run-level evidence (run.jsonl/tasks.jsonl) was never
affected. The index was reconstructed deterministically from the
campaign YAML via the runner's own `expand_plan`; the 10 surviving
original lines are byte-identical to the regenerated tail, verifying
fidelity. Original preserved as `plan.jsonl.stage6-only.bak`.

### Interim integrity log (blocks 1-3, 18/40 runs, recorded 2026-07-03)

Uniform so far: git_commit=5e5368b, ollama 0.31.1, num_ctx=8192,
repeat_distinct=1 on every task (fully deterministic), zero nemo
crashes (v1: ~11%). Strategy provenance per-run matches YAML for
blocks 1-3. No integrity concerns; final table filled at 40/40.
*(Retained as written mid-campaign. Final note: blocks 4-5 stayed
fully deterministic; block 6 (qwen3) is multi-modal — see §4.4.)*

## 1. Purpose

v1 characterized six models and left three open questions. v2 tests
the two that were turned into pre-registered strategies (coder few-shot,
nemo history-fold), measures daemon drift 0.24→0.31.1 via sentinel
cells, and characterizes qwen3:30b's bistability post-prewarm-fix. The
output is a decision: build a resolver layer, keep special cases, or
close axis 1.

## 2. Method deltas vs v1

Same probe, same params (temperature=0, seed=42, num_ctx=8192,
max_tokens=2048), same six models. Different: (a) A/B on a per-run
`strategy` field — primary comparison is within-v2, same daemon;
(b) daemon 0.31.1 (v1: 0.24) under the track-latest policy; (c) prewarm
fix live — block-firsts are clean; (d) run allocation 31 control / 9
treatment, qwen3 at 5+5 (both null; treatment half repurposed as
additional bistability repeats per Phase 2).

## 3. Reading rules (fixed in advance)

1. Strategy effect = v2-treatment vs **v2-control** (same daemon).
   v1 enters only as secondary reference, control cells only,
   steady-state cells only (v1 block-firsts were contaminated).
2. Strategy verdicts are decided by the quoted Phase 2 criteria and
   nothing else. Narrative may add color; it may not move a verdict.
3. Sentinel divergence (gemma, qwen-instruct: v1 vs v2-control) is read
   as **daemon delta** by default. It escalates to a finding only if a
   model's behavioural CLASS changes, not on count-level drift.
4. qwen3 cells are read categorically (mode distribution), not as rates.
5. loop_depth expected 0 across the board; a loop_depth pass in any
   treatment cell is promoted to a headline finding.

## 4. Results

### 4.1 Per-model three-column comparison

v1 = steady-state (block-first excluded; 15 task-cells/model, denom.
verified against regenerated `excluded_repeats`). v2 = all cells (no
exclusions needed; prewarm fix live).

| Model | Metric | v1 (steady) | v2-control | v2-treatment |
|---|---|---|---|---|
| qwen2.5-coder:7b | pass | 0/15 | 0/9 | 0/9 |
| | calls/task | 0.67 | 0.33 | 0.67 (2.0×) |
| | channel (struct/text) | 0/10 | 0/3 | 6/0 (recorded, not judged) |
| qwen2.5-coder:14b | pass | 0/15 | 0/9 | 0/9 |
| | calls/task | 0.67 | 1.00 | 2.00 (2.0×) |
| | channel (struct/text) | 0/10 | 0/9 | 18/0 (recorded, not judged) |
| mistral-nemo:12b | pass | 0/15 | 0/9 | 0/9 |
| | calls/task | 2.00 | 2.00 | 2.00 (1.0×) |
| | channel (struct/text) | 15/15 | 9/9 | 18/0 (recorded, no prediction registered) |
| qwen2.5:7b-instruct | pass | 0/15 | 0/18 | — (sentinel) |
| | calls/task · malformed | 2.00 · 0 | 2.33 · 6 | — |
| gemma4:e4b | pass | 10/15 | 12/18 | — (sentinel) |
| | calls/task | 4.00 | 4.00 | — |
| qwen3:30b | pass | 2/15 | 7/30 | — (bistability baseline) |
| | calls/task · malformed | 1.60 · 6 | 1.53 · 7 | — |

### 4.2 Strategy verdict boxes

**qwen2_5_coder** — criteria quoted from Phase 2:
> FALSIFIED if pass_rate = 0/9 AND mean tool_calls_total per task <
> 2× v2-control. SUCCESS: pass_rate ≥ 3/9. PARTIAL: 1–2/9, or 0 passes
> with ≥2× call volume.

Verdict (7b): ☐ SUCCESS **☑ PARTIAL** ☐ FALSIFIED — evidence: 0/9
passes; 0.67 vs 0.33 calls/task = 2.0× ≥ 2× (§4.1).
Verdict (14b): ☐ SUCCESS **☑ PARTIAL** ☐ FALSIFIED — evidence: 0/9
passes; 2.00 vs 1.00 = 2.0× ≥ 2× (§4.1). 14b additionally activated
loop_depth emission (0→3 calls/task) without passing it.
Size-invariance check: **holds under treatment** — both sizes moved
identically (2.0× volume, full channel flip). Controls, however,
split — see F4 (drift), not a strategy effect.
If FALSIFIED → confound arm: **not triggered.** The pre-registered
confound resolved empirically in the opposite direction: the few-shot
requests fenced-JSON text, yet treatment emission arrived 100% via the
native structured channel (24/24 calls across sizes). The template's
"no backticks" line did not suppress emission; the few-shot acts as
format-priming that the template's native instruction captures. The
Phase 2 mechanism label ("text-channel strategy") was wrong; the
channel-agnostic primary metrics were unaffected by this error — see F6.

**mistral_nemo** — criteria quoted from Phase 2:
> FALSIFIED if pass_rate = 0/9 AND mean tool_calls_total per task <
> 2× v2-control. SUCCESS: pass_rate ≥ 2/9. PARTIAL: 1/9, or 0 passes
> with ≥2× call volume → Modelfile variant as v2.1 before concluding.

Verdict: ☐ SUCCESS ☐ PARTIAL **☑ FALSIFIED** — evidence: 0/9 passes;
2.00 vs 2.00 calls/task = 1.0× < 2× (§4.1). Modelfile-v2.1 branch not
triggered (requires PARTIAL).

NOTE recorded 2026-07-03 AFTER block 3 ran (post-data; no foresight
claimed): the FALSIFIED-by-volume condition assumed the manifest drop
suppresses control volume. Block 3 shows v2-control already at 2.0
calls/task - volume is an insensitive channel for this mechanism.
Handling: the verdict above is still decided by the ORIGINAL criterion
(no post-hoc amendment). Separately, findings must record (a) the
criterion-insensitivity itself, and (b) the observed 100% channel
purification (50/50 -> 100/0 structured) as a real but EXPLORATORY
effect - never a registered success metric. Whether channel purity
alone justifies keeping the strategy is a Phase 5 judgment argued
openly on its merits, outside the falsification machinery.
*(Resolution of that judgment: see §7 and F3.)*

Mechanism note: gate was VERIFIED pre-campaign (manifest+system drop
confirmed on 0.31.1); the falsification means re-establishing them did
not move completion — the completion failure is not manifest-starvation.

### 4.3 Daemon drift (sentinels, v1-steady vs v2-control)

| Model | v1 class | v2-control class | Byte-level trajectory match | Reading |
|---|---|---|---|---|
| gemma4:e4b | structured-succeeds | structured-succeeds | **v1 == v2 byte-for-byte** (executor-verified); identical rates (2/3 pass, 4.0 calls/task) | **zero daemon drift on the anchor** |
| qwen2.5:7b-instruct | structured-fragile | structured-fragile | counts moved: calls 2.00→2.33, malformed 0→6, pass 0→0 | daemon delta, class stable |

Non-sentinel controls add resolution: nemo-control reproduces v1
exactly (2.00 calls, 50/50 split) — no drift. The coder controls moved
AND split (v1: both sizes byte-identical at 0.67; v2: 0.33 vs 1.00) —
see F4.

### 4.4 qwen3:30b bistability (10 clean-prewarm repeats)

loop_depth (fail/0 calls) and content_robustness (fail/2 calls) are
stable across all 10 repeats. small_chain is **multi-modal at fixed
seed/temp/ctx** (repeat_distinct=4):

| small_chain mode | count |
|---|---|
| fail / 0 calls | 3/10 |
| pass / 3 calls | 5/10 |
| pass / 4 calls | 1/10 |
| pass / 7 calls | 1/10 |

Conclusion: **☑ bistability survives clean prewarm** — intrinsic
(MoE-routing hypothesis stands), richer than v1's two modes (four
distinct outcome trajectories), and isolated to small_chain. The
prewarm fix removed the *artifactual* non-determinism (instruct and
gemma are byte-deterministic in v2) but not this genuine one. v1
steady 2/15 vs v2 7/30 pass is a mode-frequency difference within
what a 4-modal process produces at these sample sizes — no drift claim.

## 5. Findings

**F1 — Completion, not emission, is the wall.** Both strategies moved
emission robustly (F2, F3); neither moved pass_rate off 0/9. Across
all 120 task-cells, non-gemma models emit 0.33–2.33 calls/task against
chain lengths of 3–5 — loops die mid-chain regardless of channel.
Whatever breaks the loop after a successful call is not prompting
format. This is the axis the resolver hypothesis lived on, and the
data closes it (§7).

**F2 — qwen2_5_coder: PARTIAL at both sizes, exactly on the boundary.**
2.0× call volume at 7b and 14b, full channel flip (0% → 100%
structured), loop_depth activation on 14b — and zero completions.
Per the pre-committed outcome table: strategy kept as a special case;
no follow-up designated ("coder: none — accept").

**F3 — mistral_nemo: FALSIFIED per criterion; retained on different
grounds.** The registered capability hypothesis failed: re-establishing
system+manifest every turn did not move completion or volume. The
exploratory observation stands separately: channel purification 50/50 →
100/0 structured (18/18), removing the text-fallback parser from
nemo's critical path. Retention judgment (§7): keep the strategy as a
**template-defect workaround** — the verified mechanism (Ollama's
mistral-nemo template drops system prompt AND tool manifest whenever
the last message isn't user-role) is a correctness defect independent
of pass_rate; no Agora run should talk to this model without its
system prompt. This retention claims correctness, not capability.

**F4 — Daemon drift 0.24→0.31.1 is real, small, and localized to the
text-emission path.** The anchor (gemma, in-daemon rendering) shows
zero drift — byte-identical across daemon versions. Nemo-control is
drift-free. The drift concentrates in the coder controls: v1's
headline "7b/14b byte-identical — size does not help" **no longer
holds on 0.31.1** (0.33 vs 1.00 calls/task; 3× apart). A v1-style
finding about the model family was partially a finding about the
daemon under it. The A/B design absorbed this: had v2 compared
treatments against v1 instead of same-daemon controls, the coder
strategy effect would have been mis-estimated (7b: 0.67 vs 0.67 —
"no effect" — instead of the true 2.0×).

**F5 — qwen3:30b bistability is intrinsic.** Survives clean prewarm,
four outcome modes on small_chain at fixed seed, deterministic on the
other two tasks. MoE routing remains the standing hypothesis
(consistent with gemma4:26b-A4B being untested MoE — roster). For
orchestration: the model cannot be routed as if deterministic;
repeat-based ensembling (7/10 pass) is the only reliability lever v2
data supports.

**F6 — Pre-registration post-mortem (method finding).** Three of its
properties did the work: (a) channel-agnostic primary metrics survived
the coder mechanism being mispredicted (few-shot acted as native-format
priming, not text-channel legitimization); (b) the criterion
insensitivity on nemo was caught and quarantined rather than silently
absorbed — the verdict stands on the original criterion, the
insensitivity is recorded, and the channel effect is labeled
exploratory; (c) the drift sentinels converted a daemon upgrade from a
confound into a measurement. The reusable lesson: falsification
criteria should be stated against a *predicted control value*, not
only a ratio — nemo's criterion silently assumed a depressed control.

## 6. Caveats

**C1 — plan.jsonl reconstruction.** The campaign-root run index was
truncated to the final block by a staged-execution bug (§0). Repair
was deterministic (runner's own `expand_plan` over the frozen campaign
YAML), verified by byte-identity of the 10 surviving lines against the
regenerated tail. Per-run evidence was never affected. Code fix
tracked as a post-campaign task (merge-by-id semantics for plan.jsonl).

**C2 — nemo criterion insensitivity.** §4.2 note. The FALSIFIED verdict
is decision-valid (0 passes is 0 passes under any volume threshold)
but the volume clause carried no information for this model.

**C3 — coder mechanism relabel.** The Phase 2 mechanism description
was wrong (see F6a); metrics unaffected. The "Known confound" and its
disambiguation arm are moot — closed without running.

**C4 — sample sizes.** Determinism makes each (model, arm, task) cell
effectively n=1 (3 identical repeats), except qwen3 where n=10
characterizes a distribution. Boundary-exact results (2.0× twice)
are exact integer ratios of small counts, not estimates with error bars.

## 7. Resolver decision

Quoted gate: strong (≥2 SUCCESS) / weak (1 SUCCESS) / none (0 SUCCESS).
Observed: coder PARTIAL ×2, nemo FALSIFIED → **zero SUCCESS.**

- ☐ Strong evidence: build the resolver.
- ☐ Weak evidence: special-case the winner, defer.
- **☑ No evidence — no resolver is built.** With the F1 refinement:
  v1's coder/nemo behaviours are capability floors *for completion*,
  while emission format proved cheaply steerable — a resolver layer
  would generalize exactly the part that doesn't gate task success.
- ☐ PARTIAL branch follow-ups: coder — none designated (accepted);
  nemo Modelfile v2.1 — not triggered.

Dispositions: `qwen2_5_coder` retained as special case (F2);
`mistral_nemo` retained as correctness workaround, not capability
strategy (F3); `strategies/` stays the deliberately un-generalized
~124-SLOC mechanism it is — the "designed detour" ends here.

**Axis 1 is closed.** The successor question is F1's: what breaks the
loop after successful emission — result-feedback handling, stop
behaviour, or task-state reasoning. That is an agent-loop question,
not a prompt-format question, and belongs to the next axis design.

## Regeneration

- Vectors: `python scripts/analyze_layer2.py --campaign <dir>` per
  campaign → `<dir>/reports/capability_vectors.csv` (v1: 42 rows,
  v2: 63 rows, `campaign` + `strategy` columns).
- §4.1 table: `scripts/compare_campaigns.py` — TO BE IMPLEMENTED
  (post-campaign task, spec unchanged): read both CSVs, group by
  (model, sub_target), pivot campaign/strategy into three columns
  (v1-steady via excluded_repeats filter, v2 control, v2 treatment),
  emit markdown. Pure read → transform → print; fixture test + one
  edge case (model with no treatment cells emits "—"). Est. <100
  lines with test.
- plan.jsonl repair provenance: `plan.jsonl.stage6-only.bak` +
  byte-identity check (this document, C1).

---

## 8. Integrity addendum (2026-07-03, post-campaign): stale-output forensics

Recorded after `docs/runs/axis-1-v3/forensics-stale-out.md` (transcript-level
classification of every equality/contains task-cell, v1/v2/v3.0). A
pre-existing probe defect confounded task-success attribution in ALL
campaigns: `workspace out/` was never reset between runs, and write_file's
overwrite guard both blocked the model's write AND disabled write_file for
the remainder of the task. Predicates then evaluated STALE files.

**Retracted (attribution level):**
- All live-pass claims. Forensics: v1 = 2 live / 46 stale-backed passes;
  v2 = 0 live / 80 stale-backed; v3.0 = 0 live / 30 stale-backed. gemma's
  "12/18 anchor pass" (v1 and v2) was stale-backed in every cell — it never
  wrote live. qwen3's "pass modes" (F5, 7/30) are stale-backed as passes.
- gemma's loop_depth failure as a capability floor. All three campaigns:
  guard_artifact_fail with BYTE-CORRECT attempted content (forensics
  appendix). The claim "even the best model fails loop_depth" is withdrawn
  — it failed the guard, not the task.

**Reaffirmed (trajectory level — unaffected by file state):**
- Emission channels, call volumes, determinism-as-observed, the drift
  anchor's v1==v2 byte-identity, qwen3's trajectory multimodality.
- Both strategy verdicts (F2 PARTIAL, F3 FALSIFIED). Stale files made
  passing EASIER, and treatment cells still recorded 0 passes; the verdicts
  are conservative-safe. v2's 6 guard_artifact_fails are all gemma
  loop_depth cells; non-gemma fails are genuine.
- The resolver decision (§7) — built on trajectory metrics and zero
  treatment passes; unchanged.

**Amended:**
- F1 strengthens: completion was an even higher wall than measured — most
  recorded "completions" were not live. The two v1 live passes are the only
  live task completions in the program's history to date.
- New standing defect (integration-blocking, since scheduled): tool-result
  rendering leaks its `[read_file#N]` marker into content-fidelity tasks.
  Prefix-tolerant `contains` predicates masked this throughout.

**Provisional at time of writing (since resolved — see
docs/runs/determinism-probe/findings.md):** the v3.0.1 gemma content
anomalies (newline-stripped concat, non-identical repeats) were traced to
near-tie greedy decoding under weakly-transmitted byte information, not a
capability change.

Probe v4 (out/ reset + staged-path artifact_capture) restores intended
semantics; v4+ cells are never pooled with earlier probes.
