# Integration run 2.0 — findings (clean greenfield)

*Written 2026-07-05 after run 2.0 stopped at P5 (second red). Pre-registration:
docs/runs/integration-run-2/pre-registration.md (binding). Run 1.x findings +
F1-F14: docs/runs/integration-run-1/findings.md. Branch feat/integration-run-2
(cut off feat/integration-run-1 after run 1.5). Session log:
docs/runs/integration-run-2/session-log.md.*

## Outcome

Fresh ledger, all accumulated fixes (F1-F14) live. **P3 GREEN. P4 RED (T4.2
dropped !roll in a whole-file rewrite) -> ONE repair -> GREEN. P5 RED (first
pass 2 failed / 6 passed) -> ONE repair (help ValueError fixed) -> RED (1
failed / 7 passed) -> STOP** per world (c) (second red on the same gate,
waivers forbidden). P6/P7/P9 not reached; run did not complete; no
PROJECT_STATE.md. The stop is on a single test — roll_malformed — and the
diagnosis is a tester/spec wording divergence, not a model completeness floor.

## Findings

**F13 VERIFIED LIVE GREENFIELD — the invariant holds across a whole run.**
Every implementer task (T3.1, T4.1, T4.2, and both repairs) logged
`manifest: filtered 13 tools (allowlist)` and NONE logged `hid write_file`:
the allowlisted seat kept write_file on every existing-file modification, so
the 1.4 collision (guard hides write_file, seat has no edit family, no write
tool) cannot recur. The verifier seat (unrestricted) logged `hid write_file`
after its first verdict write — v2.4 preserved where the edit family exists.
Manifest shaping is fully observable now (the Part-6 rule). T4.1's ping smoke
passing on the P3 stub was the first live F13 win; the P4 and P5 repairs
confirmed it under repair conditions.

**F14-fresh QUANTIFIED — incremental build beats from-scratch rewrite.**
The greenfield core reached **6/8 spec behaviours on first-pass P5 and 7/8
after one repair**, versus run 1.5's 4/8 from a single from-scratch repair
rewrite. The incremental path (T4.1 router -> T4.2 roll -> tight per-task
smoke gates) produced a materially more complete implementation than one
whole-file rewrite. Direct support for the pre-registration's mitigation of
the F12xF14 tension: small tasks + tight oracles + incremental builds. The
model-completeness floor is not fixed — it is *managed down* by task design.

**F12xF14 tension observed LIVE at P4 (the whole-file-rewrite regression).**
T4.2, asked to ADD !roll to the existing core.py, rewrote the whole file (the
only affordance the measured write-only surface offers) and reproduced T4.1's
four commands while OMITTING roll entirely (grep -c roll = 0). The write-only
surface forces the rewrite; gemma's weak operation (F14) dropped the new
feature under it. Recovery was clean: both P4 smoke gates NAMED the miss
(file_contains "roll"; the roll assert), the oracle was expressive (F7), and
ONE repair landed roll while keeping the other commands. So the tension is
real and costs one repair per incremental feature, but the named-oracle repair
loop absorbs it. (Stage-3 resolution — evidence-based narrow-edit affordances
per model — stands as the durable fix; recorded in the pre-registration.)

**F15 — the phase gate is sensitive to SPEC UNDERSPECIFICATION via tester
brittleness.** The spec says only "malformed spec -> a usage message" — no
exact text. The tester (T5.1) rendered this as a literal-substring assertion:
`assert "usage message" in result or "invalid roll specification" in result`.
The implementer wrote a semantically-correct usage message: `"Malformed roll
specification: invalid. Use format NdM."` — which contains neither literal.
Neither party is "wrong" against the human spec; the gate reds on a wording
mismatch. This is the mirror image of F6 (spec-channel starvation): there the
tester had too little spec; here the spec ITSELF is too loose, and two
faithful readings diverge. Consequence for doctrine: an underspecified
acceptance point produces a red gate that no repair can reliably close,
because the "defect" is a contract ambiguity, not an artifact flaw. Fixes
(run-2.x / spec doctrine, owner's call): (a) tighten the spec's malformed-roll
line to name the required substring, OR (b) instruct testers to assert
SEMANTIC properties (a non-empty error naming the command) rather than literal
marketing strings, OR (c) treat a stable near-miss (7/8, the last failure a
wording divergence) as a human-adjudication point rather than an automatic
stop. Waivers remain forbidden by protocol, so run 2.0 stops as designed and
the call is chat-side.

**Verifier fidelity — V5.1 now WRITES verdicts/p5.json (first ever), but as
malformed JSON.** The item-5 write_file instruction finally reached the P5
verifier: verdicts/p5.json exists this run (run 1.x never produced it). But
its content is a Python-dict repr (single quotes), so the parse-gate reds
(JSONDecodeError). Progress (artifact produced) with a residual (not valid
JSON). Non-gating. Sub-fix candidate: the verifier instruction should show a
valid-JSON example, or the verdict should be emitted via a structured tool.
V4.1 (P4) produced VALID JSON, so the failure is not universal — instruct is
inconsistent across phases.

**Nudge / truncation accounting.** P3: 1 nudge (empty-turn stall recovery,
S2). P4/P5 tasks: 0 nudges. No stdout truncation events this run (the pytest
outputs — 2 and 1 failures — stayed under the 4 KB head+tail bound; F11 not
stressed). Infra held throughout; Ollama 0.31.1, gemma4:e4b + qwen2.5:7b.

## Disposition

Run 2.0 is a SUCCESS of the instrument even though it stopped: it proved the
full accumulated fix-stack live (F13 greenfield, incremental-build completeness
gain, named-oracle repair at two phases), and surfaced F15 (spec
underspecification x tester brittleness) as the true blocker — a spec/test
doctrine issue, not a model or framework defect. The single remaining P5
failure is a one-line wording reconciliation.

**Next (owner's call, chat-side):** the honest options are (i) tighten the
malformed-roll spec/test wording and re-run 2.1 from P5 to carry the run
through P6/P7/P9 (first-ever adapter + acceptance gates, still unexercised);
or (ii) accept F15 as a spec-doctrine finding and fold the wording fix into the
run-2 spec before a 2.1. Either way the deferred run-1 measurement
(PROJECT_STATE.md human fact-check) waits on a completed run. P6/P7 remain the
first-unexercised frontier.

---

## Run 2.1 addendum (2026-07-05) — see run-1 findings Part 9 for the full analysis

Run 2.1 applied the F15 acceptance predicate (T5.1 inline contract: usage
message MUST contain "NdM") and re-established P5. Result — the furthest point in
program history:
- **P5 GREEN** (world (a): tester wrote `assert result is not None and "NdM" in
  result`, citing F15 — predicate-conformance exact; zero implementation change).
- **P6 GREEN, first-ever** (T6.1 wrote the __main__ adapter; suite still 8/8).
- **P7 RED, first-ever -> repair -> RED -> STOP.** F16: the adapter never imports
  handle_message (T6.1's description omits the import contract) AND T6.1's gate is
  only `pytest -q` (never runs __main__) — F6/F8 + F10 recurrence at the adapter.
  F17: the adapter's `except NameError: pass` swallowed the failure, so the P7
  oracle carried only "no stdout", not the NameError — defensive swallowing
  degrades oracle expressiveness. P9 not reached; no PROJECT_STATE.md.

**Run 2.2 (pre-registered, run-1 findings Part 9):** T6.1 gains the inline import
contract + a `python -m echobot` stdin smoke gate (F16 fix); conditions-defect
re-establishment of P6, then --next through P7/P9.
