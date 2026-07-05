# Integration run 1 — findings and run 1.1 pre-registration

*Findings written 2026-07-03 after forensic analysis of the stopped run
(session log b117471, provenance runs_out/integration-run-1/). Run 1.1
pre-registration recorded BEFORE its execution, per program practice.*

## Part 1 — run 1 findings

**Outcome:** P3 green, P4 red→green (1 repair), P5 red→red, protocol
stop. The stop was correct and the instrument did its job: the failure
found was structural, framework-side, and is now caught at load time.

**F1 — root cause: two role systems collided.** All 17 write_file calls
across both T5.1 attempts died on `inner_tools._enforce_path_scope`
(v2.7 turf rule: "implementer role may not write tests/ — tests/ is
owned by the tester"), a rule built for the legacy
architect/implementer/tester/reviewer pipeline to stop implementers
from editing authoritative contract tests. The run-1 flow assigned test
AUTHORING to the implementer seat, and the new casting taxonomy
(roles.yaml) had no tester. Deterministic rejection in every process.
**The model is exonerated:** 8-9 structured, well-formed writes, one
nudge, then resignation via mark_complete is reasonable behaviour under
guidance that was unfollowable for the task ("fix your implementation,
not the test" — with nothing to fix and no tester to defer to).

**F2 — the checkable gap.** Task-vs-permission feasibility ("can the
assigned role write this task's output_path?") was statically checkable
and unchecked; the human plan-lint verified spec fidelity, not tool
permissions. Fixed: load_flow now fails loudly on role-scope violations
(f7d8d70), rules factored into agora/core/role_scope.py shared with the
enforcement point so lint and runtime cannot drift. The pre-fix flow
fails its own lint — the class is dead at load time.

**F3 — observability.** The phased runner ran observer-off; the
rejection message that explains everything was generated 17 times and
discarded 17 times. Both the executor and the reviewer missed this at
review. Fixed: run.log attached per phase invocation (f7d8d70).
Doctrine reaffirmed the hard way: errors must leave evidence, in every
runner, always.

**F4 — latent and preserved: P4 signature drift.** T4.1 produced
`handle_message(self, message)` with `self.rng` (class-shaped) against
the spec's `handle_message(text, rng)`. The P4 gate (import +
file_contains) was accepted as weak at plan-lint on the argument that
P5 verifies behaviour — the scope bug prevented that demonstration.
The drift is intact in the workspace and is run 1.1's experiment.

**F5 — verifier notes.** The P3 verifier emitted verdict "pending" —
correctly failed by the JSON parse-assert gate (non-blocking; the
upgraded lint gate paid off). Structural: verifiers that depend_on
blocking tasks never produce verdicts when those tasks fail —
calibration data exists only for green phases. Design note for a later
flow revision: verifier tasks should run at gate time unconditionally.

**Also observed (run-1 positives):** no overwrite-guard friction (the
predicted P4 stub-rewrite friction did not materialize — the model went
straight to edit tools); the P4 repair succeeded on a bare
predicate-name oracle; no truncation events; infra held throughout.

## Part 2 — run 1.1 pre-registration

**Conditions delta vs run 1:** tester seat (gemma, turf-separated) owns
T5.1; observer ON; role-scope lint active; cross-phase --rerun-task
available; commit f7d8d70.

**Workspace: RESUMED, not reset.** P3/P4 gates stand in the ledger; the
drifted core.py remains on disk deliberately — it is the F4 experiment.
Run 1.1 is a continuation in the same output dir; the ledger's
red -> (1.1) -> outcome sequence is the record working as designed.

**P5 predictions — two worlds, both registered:**
- (a) *Spec-faithful tester:* tests/test_core.py lands with the 8 named
  tests written against `handle_message(text, rng)`; pytest goes RED on
  the P4 drift (TypeError/signature errors). The repair cell activates:
  designated repair = `--rerun-task T4.1 --oracle P5` (cross-phase);
  prediction: the implementer refactors class -> function under the
  verbatim TypeError oracle; mechanical re-eval of P5 goes green.
- (b) *Implementation-following tester:* the tester reads core.py
  (reading src is permitted; writing is not) and writes tests matching
  the drifted API; pytest goes GREEN and the drift is laundered into
  the tests. Registered consequence: the drift is then caught at P6/P7
  (the spec-shaped __main__ adapter or the acceptance stdin checks fail
  against a class-shaped core) — the phase-depth design predicts drift
  cannot escape the run, only move. World (b) additionally logs a
  tester-fidelity finding: tests followed code, not spec.

**Repair protocol:** budget reset under the new conditions (one repair
per gate; second red on the same gate stops the run). Waivers remain
forbidden.

**Interpretation rules, fixed now:** mechanical `reevaluate_phase_gate`
records are read as ARTIFACT-STATE checks — `mark_complete_called` in a
re-eval is provenance-carried, not re-earned; the genuine signal in any
re-evaluated gate is its run_check re-execution. Re-eval records should
be visually distinguishable in the ledger (see execution brief).

**Success shape for run 1.1:** P5 through P9 green (with or without the
registered repair), PROJECT_STATE.md produced and human fact-checked,
repair-doctrine cell filled with a genuine defect. Any stop follows the
same protocol as run 1 and produces its findings the same way.
