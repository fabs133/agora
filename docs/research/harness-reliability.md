# Harness reliability — v3 pre-registration and integration gate

*Pre-registered 2026-07-03, before any v3 run executes. Companion to
`campaigns/axis-1-v3.0.yaml` (draft, unrun at time of writing) and
`docs/runs/axis-1-tool-call-fidelity-v2/findings.md` (F1, autopsy).
Baselines referenced below are v2 cells (same daemon 0.31.1).*

## Integration-readiness gate (the point of the whole program)

Evaluated at the **v3.2 production configuration**
(`tool_errors: corrective`, `nudge_budget: 1`), not at v3.0:

> **≥ 2 model profiles achieve 9/9 task-cells (3 tasks × 3 repeats),
> at least one of them ≤ 12 GB class.**

gemma4:e4b (9.6 GB) qualifies for the size clause. Profiles passing the
gate become the implementer-eligible pool in `profiles.yaml` (explicit
flag, comment citing this document); integration testing proceeds with
that pool only. qwen3:30b is gate-exempt and ensemble-only regardless of
its v3 numbers (intrinsic multimodality, v2 findings F5).

## Variable accounting (what compares to what)

- small_chain, content_robustness: prompts unchanged from v2 →
  v3.0-vs-v2 is a SINGLE-variable comparison (tool_errors raw→corrective).
- loop_depth: TWO bundled changes (corrective errors + the probe-v3
  byte-exactness sentence). Attribution rule: gemma's v2 mark_complete
  calls were all summary_ok — corrective errors are inert for gemma —
  so any gemma loop_depth movement attributes to the prompt sentence
  alone. For the other models, loop_depth failures in v2 occurred
  before the write step; loop_depth movement there is read jointly and
  disambiguated by transcript + artifact_capture, not assumed.
- probe_version=3 is carried in provenance; v3 cells are never pooled
  with v1/v2 cells in any table.

## v3.0 pre-registered expectations (per model, vs v2 baseline)

**qwen2.5:7b-instruct — the headline prediction of the stage.**
v2: deterministic; small_chain died on mark_complete(path, content) →
KeyError('summary') → empty turn, WITH the correct output bytes already
composed (r019 anchor). Prediction: with the corrective message naming
write_file, **small_chain flips to 3/3 pass.** loop_depth and
content_robustness: NO change expected (their v2 failures fired no tool
error — silent termination at the write boundary; corrective errors have
nothing to correct). Predicted stage outcome: 3/9 (from 0/9).
FALSIFIED if small_chain stays 0/3: S1 was not the binding constraint
even for the cleanest near-miss in the dataset → skip v3.1, go directly
to v3.2 (silent termination dominates everywhere).

**qwen3:30b — categorical read only** (multimodal, n=3 is too small for
rates; v2 reading rule 4 carries over). Expectations: malformed
mark_complete count drops to ~0 (corrective validation catches the
write_file_args pattern pre-dispatch); pass count does not DECREASE.
No numeric success threshold is registered; this model informs the
malformed-conversion question, not the gate.

**qwen2.5-coder:14b (strategy: qwen2_5_coder) — no-change control.**
v2 treatment fired zero tool errors (failures are 100% prose_no_call;
it never calls mark_complete). Prediction: 0/9, unchanged calls/task
(2.00). Any movement here is unexplained by S1 and gets flagged, not
celebrated.

**mistral-nemo:12b (strategy: mistral_nemo) — no-change control.**
Same logic: v2 treatment died silently at the write boundary, no errors
fired. Prediction: 0/9, 2.00 calls/task, 100% structured (the retained
workaround keeps doing its correctness job).

**gemma4:e4b — anchor + the S4 diagnostic.**
small_chain, content_robustness: 3/3 each, byte-identical trajectories
(corrective errors must not perturb an already-passing model — if they
do, the mechanism has a leak). loop_depth carries the two pre-registered
readings: (a) flips to 3/3 → the v2 failure was spec imprecision; the
"capability floor" shrinks again and gemma is 9/9 already at v3.0;
(b) stays 0/3 → artifact_capture now yields the actual bytes and the
follow-up is designed from the diff, not from guesses. Either outcome
is informative; (b) with NO capture (file unwritten) would contradict
the v2 protocol data and triggers a data-integrity check.

## Stage decision rules (pre-committed)

- Instruct small_chain flips → S1 confirmed binding. Proceed to v3.1
  (preemptive protocol line in the system prompt) to measure
  preemptive-vs-reactive information delivery on the models that still
  fail silently.
- Instruct small_chain does not flip → S1 refuted as binding constraint;
  v3.1 is skipped (preemptive info is weaker than reactive info that
  already failed); proceed directly to v3.2 (nudge_budget: 1, the S2
  mechanism) — silent termination is then the dominant residual and the
  nudge is the registered lever against it.
- gemma perturbed on the unchanged tasks → stop; mechanism leak; no
  further stages until explained.
- Gate evaluation happens only at v3.2 config. v3.0/v3.1 numbers never
  substitute.

## Assumptions on record

Gate numbers (≥2 profiles, 9/9, ≥1 ≤12 GB) were proposed 2026-07-03 and
accepted by project owner sign-off on the program plan; adjust here
BEFORE v3.2 executes if they should move. The ≤12 GB clause exists
because the eligible pool must be runnable alongside a second resident
model on the 24 GB P40 during integration testing.

---

## v3.0 / v3.0.1 resolution notes (2026-07-03)

v3.0: instruct small_chain did NOT flip (correction delivered, model
silent-terminated) -> per the pre-committed rule, v3.1 is SKIPPED; v3.2 is
next. qwen3 malformed 7->0 as expected. Controls unchanged. gemma anchor:
superseded by the stale-output forensics — see the v2 findings Integrity
addendum (§8). The gemma loop_depth reading (b) occurred in v3.0.1 with a
twist: artifact_capture worked, but the captured content contradicts
v3.0's byte-correct attempt at identical prompt+daemon, and repeats were
non-identical. The determinism investigation gates any v4 capability
claims and precedes v3.2 execution.

## v3.2 resolution note (2026-07-05) — S2 mis-targeted; gate NOT met

Full report: [`docs/runs/axis-1-v3/v3.2-findings.md`](../runs/axis-1-v3/v3.2-findings.md).
Ran at the production config (`corrective`, `nudge_budget: 1`) **under
probe v7** — a substrate this pre-registration predates (v3.0 was probe
v3). That substrate change is decisive:

- **Headline falsified as an S2 result.** instruct small_chain flipped
  0/3 → 3/3, but the nudge fired **0 times**. The flip is entirely probe
  v7's rendering fix (form-B bare tool messages + LF seeds): instruct now
  writes 62 bytes with real newlines and calls mark_complete on turn 2 —
  no silent-terminate turn exists for S2 to catch. The registered lever
  was inert on the cell it was registered for.
- **S2 earns no pass anywhere.** Nudges fired only on qwen3 (4×) and coder
  (6×, all loop_depth). Where it fired it re-activated the model
  (empty turn → nudge → read_file → mark_complete) but the retried bytes
  were still wrong (coder loop_depth 0/3). Most silent-terminators
  (instruct loop_depth, nemo) re-call mark_complete every turn and never
  emit the 0-tool-call turn S2 keys on — structurally inert.
- **Diagnosis.** v7 converted the dominant failure from *silent
  termination* into *confident wrong-byte completion* (trailing-newline
  off-by-one, spurious separator, dropped operand, one literal-escape
  holdout in nemo). S2 targets the empty turn; the residual is a byte
  error. Mis-targeted by construction, post-v7.
- **Gate: NOT met.** gemma-e4b is the only 9/9 (≤12 GB). No second profile
  reaches 9/9 (instruct 6/9, coder 3/9, qwen3 4/9, nemo 0/9). The nudge
  did not lift a second model across the line.

The S2 line is closed. The registered next lever must target the actual
post-v7 residual (tail-newline / separator byte errors on concat+redirect),
which is a validation/prompt-shaped intervention — a **new**
pre-registration, not a continuation of this one.

---

## v8 pre-registration: completion review (S6) — FINAL lever of this phase

*Registered 2026-07-03, before implementation or any run. Stopping rule,
pre-committed: after v8 the gate is evaluated with whatever pool exists —
met, or formally renegotiated (gemma + qwen3-ensemble). No v9 lever.*

Mechanism: on a VALID mark_complete with review_budget > 0 (default 0),
harness injects the written output file(s) as a tool result — verbatim
bytes through the v7-transparent channel, prefixed `<path> (<N> bytes):`
— plus one line asking to confirm completion or revise first. One round;
then completion proceeds regardless. Production-valid (reflection, no
oracle). Known blind spots, accepted: never fires without mark_complete
(coder content_robustness omission unreachable); cannot help models that
never reach a valid completion.

Per-model expectations (v8 = v3.2 config + review_budget 1, 5 blocks x 3):
- instruct: loop_depth (trailing-newline off-by-one) is the review's
  center-of-mass target. Prediction: loop_depth >=2/3 -> instruct 9/9 ->
  GATE MET (two profiles <=12 GB). FALSIFIED for instruct if loop_depth
  stays 0/3 with the review observed-and-ignored (confirm-without-fix).
- gemma: MUST stay 9/9 (review fires on its valid completions; safety
  check = the review does not perturb a passing model). Any regression
  halts interpretation pending mechanism review.
- nemo: dropped-operand and off-by-one errors are visible in read-back;
  directional improvement plausible, no numeric threshold registered
  (0/9 baseline, weakest protocol discipline in the pool).
- coder-14b: small_chain stays 3/3; content_robustness stays 0/3 BY
  CONSTRUCTION (blind spot above) — recorded as the residual
  completion-signal problem, not a review failure.
- qwen3: categorical read only (multimodal; repeat_distinct 2/3 at v3.2).

Gate evaluation after v8, pre-committed outcomes:
- instruct reaches 9/9 -> gate MET -> routing flags to profiles.yaml,
  integration phase opens with {gemma, instruct}, qwen3 ensemble-optional.
- instruct does not -> gate NOT met -> renegotiation decision (single
  profile + ensemble vs. gate revision) goes to the project owner with
  the full grid; no further levers.

## v8 resolution note (2026-07-05) — S6 observed-and-ignored; gate NOT met

Full report: [`docs/runs/axis-1-v8/findings.md`](../runs/axis-1-v8/findings.md).
Ran at the pre-registered config (`corrective`, `nudge_budget: 0`,
`review_budget: 1`) under probe v7, one variable vs the v3.2 baseline.

- **Gate-deciding cell falsified exactly as written.** instruct loop_depth
  stayed **0/3**: the review fired 3/3 and on all three the model, handed
  its own 54-byte output (missing the trailing `\n`) verbatim, called
  mark_complete again unchanged (`post_review_action = confirm`). The
  reflection surface works; the model cannot self-detect a one-byte tail
  error even when shown it. instruct 6/9.
- **Do-no-harm holds.** gemma stayed **9/9**; the review fired on all its
  valid completions and never perturbed a passing file. `review_budget=0`
  constructs nothing (byte-identical to v3.2). The mechanism is
  production-valid.
- **New structural blind spot.** content_robustness records
  `post_review_action = None` for ALL five profiles: the redirect stage's
  max_iterations is 4, models spend all 4 turns before mark_complete, so
  the review fires on the last turn with no runway to be consumed. A
  read-back lever needs a spare post-completion turn.
- **Gate: NOT met.** gemma-e4b remains the only 9/9; instruct 6/9, and
  qwen3 / nemo / coder all 0/9. S6 lifted no second model.

Per the pre-committed stopping rule, this phase is closed: **no v9 lever.**
The gate decision — single-profile (gemma) + qwen3 ensemble-optional vs.
gate revision — goes to the project owner with the full v8 grid. The
residual is a model byte-level self-perception limit, not a
harness-recoverable class; no in-loop reflection surface reaches it.
