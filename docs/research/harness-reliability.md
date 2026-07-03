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
