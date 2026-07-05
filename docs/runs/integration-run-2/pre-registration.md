# Integration run 2.0 — pre-registration (clean greenfield)

*Registered 2026-07-05, before execution. Run 1.x is CLOSED (findings
Parts 1-7, F1-F14). Run 2.0 is the pre-committed clean greenfield run
under ALL accumulated fixes. The run-1 workspace is retired untouched as
forensic evidence.*

## Conditions

Fresh workspace, own output dir (runs_out/integration-run-2/), new
campaign YAML, same flow file as amended through 1.5: contract-inline
T4.x + T5.1, behavioural smoke gates (F10), import gate (F7),
order_after verifiers (F5), T9.2 headers inline (F6), allowlisted
implementer seat = measured surface (F12), F13 guard invariant,
head+tail oracle capture (F11), F9 authority template, manifest-delta
logging. Harness {corrective, nudge 1, review 0}. Cast p40-24gb.
Protocol: standard — one repair per gate, second red on the same gate
stops, waivers forbidden, full P3 -> P9.

## Pre-registered expectations

- **P3**: green (proven twice).
- **P4 — the F14-fresh measurement**: does incremental build (T4.1
  router, then T4.2 roll) under an inline contract produce complete
  behaviours? The smoke gates cover ping/roll only — partial
  completeness MAY pass P4 by design; P5 owns full behaviour. Also the
  first greenfield live exercise of the F13 invariant: T4.x modify the
  non-empty P3 stub with write_file(force) on the allowlisted seat.
- **P5 worlds**: (a) green — core complete, tester rewrites
  spec-faithful tests fresh; proceed. (b) red on completeness gaps ->
  ONE --rerun-task T4.1 --oracle P5; the 1.5-validated loop plus the
  F7 evidence (narrow named fixes land) predicts green-after-repair.
  (c) second red -> STOP; F14 quantified greenfield.
- **P6/P7 — first-ever exercise** of the adapter task and the stdin
  acceptance gates. Watchlist: __main__ stub rewrite (F13 path again),
  adapter honouring the spec's default-rng behaviour.
- **P9**: README + PROJECT_STATE.md (headers inline). On completion:
  the deferred run-1 measurement runs — human fact-check of the file
  map and verification record ("can the implementer describe its own
  project").

## Success shape

Completed run + fact-checked PROJECT_STATE.md unlocks the designed
brownfield probe (extension spec: new commands + Discord adapter
against a fake gateway; phase-0 re-validation opened with a deliberate
stale-file red-team). Any stop follows the standard protocol and
produces findings the standard way.

## Recorded tension (not a 2.0 change)

F12 x F14: the measured (write-only) surface forces whole-file rewrites
— the operation F14 identifies as gemma's weak one — while its strong
operation (narrow oracle-named edits) requires the unmeasured edit-tool
family. Resolution path is Stage-3: benchmark batteries sweep the
edit-tool family per model so seats can earn narrow-edit affordances on
evidence. Until then, task design compensates: small tasks, tight
oracles, incremental builds.
