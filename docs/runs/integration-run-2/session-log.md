# Integration run 2.0 — echobot clean greenfield — session log

*Verbatim execution log. Pre-registration:
docs/runs/integration-run-2/pre-registration.md (binding, F1-F14 accumulated
fixes). Run 1.x is CLOSED. Branch feat/integration-run-2 (cut off
feat/integration-run-1 after run 1.5). Campaign: campaigns/integration-run-2.yaml.
Provenance (untracked): runs_out/integration-run-2/.*

## Pre-flight (2026-07-05) @ run-2 prep committed (suite green 1449, ruff clean)
```
ollama /api/version: {"version":"0.31.1"}; gemma4:e4b + qwen2.5:7b-instruct resident
campaign: integration-run-2.yaml — same flow (amended through 1.5), fresh output_dir
  runs_out/integration-run-2/ (fresh workspace at .../echobot), harness {corrective,
  nudge 1, review 0}, cast p40-24gb, params identical to run 1 (temp 0, seed 42, ctx 8192).
ledger (--status on the fresh dir): P3..P9 all PENDING, next=run P3 — no run-1 bleed.
```
Execution: full P3 -> P9 via `--next` per phase; standard protocol (one repair per
gate, second red on the same gate stops, waivers forbidden). Gate reports below.

## P3 scaffold — GREEN (2026-07-05)
```
=== phase P3 gate: GREEN ===
  nudge accounting: 1 fired (budget 1 - v3.2 erratum: stall-recovery)
  [PASS] T3.1 (block)
      ok  artifact_contains_echobot___init__.py / core.py / __main__.py / requirements.txt
      ok  run_check_python_-c_import_echobot  -> exit 0
      ok  mark_complete_called
  [FAIL] V3.1 (nonblock)  -> verdicts/p3.json absent (verifier-fidelity gap, non-gating)
```
Gate GREEN (all blocking green; V3.1 non-blocking). Scaffold built clean; one
nudge (empty-turn stall recovery, S2). No truncation. As pre-registered (P3 green,
proven a third time). V3.1: instruct still emits no verdict artifact — recorded,
gates nothing.

## P4 implement core — RED (T4.2 dropped !roll in a whole-file rewrite) (2026-07-05)
```
=== phase P4 gate: RED ===
  blockers: T4.2
  nudge accounting: 0 fired
  [PASS] T4.1 (block)   ping/echo/help/unknown router
      ok  file_contains "def handle_message"
      ok  run_check import handle_message -> exit 0
      ok  run_check assert handle_message('!ping', Random(0)) == 'pong' -> exit 0   [F10 ping smoke GREEN]
      ok  mark_complete   (tools_used=['mark_complete','read_file','write_file'], 13 iters)
  [FAIL] T4.2 (block)   add !roll
      FAIL file_contains "roll"                                         <- core.py has NO "roll" at all
      ok  run_check import handle_message -> exit 0
      FAIL run_check assert 'rolled 2d6:' in handle_message('!roll 2d6', Random(0)) -> AssertionError
      ok  mark_complete   (tools_used=['read_file','write_file'], 3 iters)
  [PASS] V4.1 (nonblock) -> verdicts/p4.json WRITTEN and valid (verifier wrote its verdict this run)
```
### F13 invariant — first greenfield exercise, VERIFIED (item-3 evidence, quoted)
```
manifest: filtered 13 tools (allowlist) task=T3.1
manifest: filtered 13 tools (allowlist) task=T4.1     (NO "hid write_file" — F13 kept it on the P3 stub)
manifest: filtered 13 tools (allowlist) task=T4.2     (NO "hid write_file" — F13 kept it on T4.1's file)
manifest: hid write_file (overwrite guard) task=V4.1 turn=2..7   (verifier seat UNRESTRICTED -> v2.4 hide preserved)
```
The allowlisted implementer keeps write_file on an existing file (F13); the
unrestricted verifier still gets write_file hidden after its first write (v2.4).
Both behaviours correct, both now logged. T4.1's ping smoke passing IS the first
live F13 win: write_file(force) modified the non-empty P3 stub.

### The T4.2 defect: whole-file-rewrite regression (F12 x F14 tension, live)
T4.2 rewrote core.py to add !roll and instead reproduced T4.1's four commands
verbatim and OMITTED roll entirely (grep -c roll = 0). The measured (write-only)
surface forces a whole-file rewrite to add one command; gemma's weak operation
(F14) dropped the new feature under the rewrite. Both P4 smoke gates NAME it
(file_contains "roll"; the roll assert), so the oracle is expressive (F7).
Designated repair per standard protocol: ONE `--rerun-task T4.2 --oracle P4`.

### P4 repair — GREEN (1 repair, within budget)
```
=== phase P4 gate: GREEN ===
  [PASS] T4.2 (block)
      ok  file_contains "roll"                                          <- roll now present (13 occurrences)
      ok  run_check assert 'rolled 2d6:' in handle_message('!roll 2d6', Random(0)) -> exit 0
      ok  mark_complete   (tools_used=['mark_complete','read_file','write_file'], 13 iters)
  [PASS] V4.1 (nonblock) -> verdicts/p4.json valid
```
The oracle NAMED the missing feature ("roll"), and the repair landed it: gemma
re-read core.py and rewrote it via write_file KEEPING all four prior commands AND
adding roll — the F7 (nameable defect → repairable) + F13 (write_file offered)
combination working incrementally at P4, mirroring the run-1.5 P5 repair. Manifest
log: `filtered 13 tools (allowlist) task=T4.2`, no `hid write_file` (F13). **P4:
red -> 1 repair -> GREEN.** Budget spent for P4; a second P4 red would have stopped.

## P5 tests — RED first pass (F14-fresh: 6/8 behaviours correct) (2026-07-05)
```
=== phase P5 gate: RED ===
  blockers: T5.1
  [FAIL] T5.1 (block)
      ok  8 named-test file_contains checks; collect-only -q exit 0
      FAIL run_check python -m pytest -q -> exit 1
        2 failed, 6 passed
        FAILED test_help_lists_all_commands - ValueError: too many values to unpack (expected 2)
            core.py:33  return "\n".join(f"{cmd:<6}: {desc}" for cmd, desc in help_message)  <- flat list unpacked as pairs
        FAILED test_roll_malformed - AssertionError (usage-message string mismatch)
  [FAIL] V5.1 (nonblock) -> verdicts/p5.json WRITTEN this run (first ever) but MALFORMED JSON
        (JSONDecodeError: property name not double-quoted — a Python-dict repr, not JSON)
```
### F14-fresh measurement (item 3) — incremental build beats the from-scratch rewrite
First-pass P5 on the greenfield core: **6 of 8 spec behaviours correct** — ping,
echo, echo_preserves_spacing, roll_deterministic, unknown_command,
non_command_returns_none all PASS. Only help (a ValueError crash from a flat
help-list unpacked as (cmd,desc) pairs) and roll_malformed (usage string) fail.
Compare run 1.5's from-scratch REPAIR rewrite: 4/8. The greenfield INCREMENTAL
build (T4.1 router → T4.2 roll → P4 repair, each a tight task) produced a more
complete implementation than a single from-scratch rewrite — direct support for
the pre-registration's "task design compensates: small tasks, tight oracles,
incremental builds" (the F12×F14 tension mitigation).
### V5.1 verdict artifact — deferred run-1 item, now PARTIALLY resolved
verdicts/p5.json now EXISTS (V5.1 wrote it — the item-5 write_file instruction
reaching the P5 verifier at last), but its content is malformed JSON (dict repr,
single quotes), so the parse-gate reds. Non-gating; recorded. New verifier-fidelity
sub-finding: instruct writes the file but not valid JSON.
### P5 repair — pre-registered world (b): ONE `--rerun-task T4.1 --oracle P5`
```
=== phase P5 gate: RED (mechanical re-eval) ===
  [FAIL] T5.1 (block)
      FAIL run_check python -m pytest -q -> exit 1
        1 failed, 7 passed         (was 2 failed / 6 passed — help ValueError FIXED)
        FAILED test_roll_malformed - AssertionError:
          assert ('usage message' in 'Malformed roll specification: invalid. Use format NdM.'
                  or 'invalid roll specification' in 'Malformed roll specification: invalid. Use format NdM.')
  [FAIL] V5.1 (nonblock) -> verdicts/p5.json still malformed JSON
```
Repair: T4.1 rerun, tools_used=['mark_complete','read_file','write_file'], 10 iters,
status=passed (own gate green). Manifest: `filtered 13 tools (allowlist) task=T4.1`,
no `hid write_file` (F13 kept it — write_file rewrote core.py). The help ValueError
is gone; the gate went 2→1 failed / 6→7 passed.

### The residual is a TESTER/SPEC divergence, not an implementation gap (finding)
`test_roll_malformed` asserts the LITERAL substrings `"usage message"` or
`"invalid roll specification"`. gemma returns `"Malformed roll specification:
invalid. Use format NdM."` — a semantically-correct usage message that contains
NEITHER literal. The spec says only "a malformed spec -> a usage message"
(underspecified — no exact text). Both readings are defensible: the tester chose a
brittle literal assertion; the implementer wrote a semantic equivalent. The gate
reds on a wording mismatch neither party got "wrong" per the human spec.

### STOP — second P5 red (world (c)): F14 quantified greenfield
Per the standard protocol + pre-registration world (c): first-pass P5 red (2
failed) -> ONE repair -> red (1 failed) = second red on the same gate -> STOP,
waivers forbidden. **F14 quantified for the greenfield build: 7/8 spec behaviours
green after one repair** (vs run-1.5's 4/8 from-scratch repair), the residual a
single roll_malformed tester/spec wording divergence — NOT a model completeness
floor. P6/P7/P9 not reached; run 2.0 did not complete; no PROJECT_STATE.md.

### Phase gate ledger (run 2.0)
```
P3 GREEN | P4 RED->(repair T4.2)->GREEN | P5 RED(2 fail)->(repair T4.1)->RED(1 fail) -> STOP
```

**RUN 2.0 STOPPED at P5 (second red on the same gate; 7/8 behaviours, residual =
roll_malformed tester/spec wording divergence). P6/P7/P9 not reached. No
PROJECT_STATE.md (run did not complete). No waiver.**

### F13 invariant — fully exercised greenfield (item-3 summary)
Every implementer task (T3.1, T4.1, T4.2, T4.2-repair, T4.1-P5-repair) logged
`manifest: filtered 13 tools (allowlist)` and NONE logged `hid write_file` — the
allowlisted seat kept write_file on every existing-file modification (F13 holds
across the whole run). The verifier seat (unrestricted) logged `hid write_file`
after its first verdict write (v2.4 preserved). The 1.4 collision is dead;
manifest shaping is fully observable.

### Final workspace tree (runs_out/integration-run-2/echobot/echobot, git/pycache elided)
```
README.md
echobot/__init__.py
echobot/__main__.py
echobot/core.py            (module-level handle_message(text, rng); 7/8 tests pass; roll_malformed wording ≠ tester's literal)
requirements.txt
tests/test_core.py
verdicts/p4.json           (V4.1 — valid)
verdicts/p5.json           (V5.1 — WRITTEN this run, but malformed JSON)
```
PROJECT_STATE.md: NOT PRESENT (P9 not reached — run did not complete).

---

# RUN 2.1 — conditions-defect re-establishment of P5 + first-ever P6/P7/P9 (F15 predicate applied)

## Pre-flight (2026-07-05) @ run-2.1 prep (suite green 1449, ruff clean)
```
ollama /api/version: {"version":"0.31.1"}; gemma4:e4b + qwen2.5:7b-instruct resident
conditions delta (F15): spec malformed-roll line amended chat-side to "usage message that MUST
  contain the substring 'NdM'"; T5.1 inline contract updated to match verbatim (one line). Nothing
  else in the flow changes. core.py UNTOUCHED (gemma's live output "... Use format NdM." already conforms).
ledger: P3/P4 green, P5 red (run-2.0 second red, roll_malformed wording divergence).
action: conditions-defect re-establishment — `--rerun-task T5.1` (no repair budget consumed per the
  standing rule; the 2.0 red was a VERIFIED spec defect); then --next through P6/P7/P9.
```

## P5 re-establishment — GREEN (world (a): tester conforms to the F15 predicate) (2026-07-05)
```
=== phase P5 gate: GREEN ===
  [PASS] T5.1 (block)  — 8 named tests present; collect-only exit 0; pytest -q: 8 passed
  [FAIL] V5.1 (nonblock) -> verdicts/p5.json still malformed JSON (dict repr) — non-gating
```
### Tester's regenerated roll_malformed assertion (item-3 (a)/(b) discriminator, quoted)
```python
def test_roll_malformed():
    text = "!roll invalid spec"
    result = handle_message(text, get_seeded_rng())
    # Per functional contract (F15), the usage message MUST contain "NdM".
    assert result is not None and "NdM" in result
```
**WORLD (a):** the tester asserted the STATED predicate (`"NdM" in result`),
citing F15 by name — no deviation. The 2.0 blocker was purely the contract
ambiguity; naming the acceptance predicate resolved it with zero implementation
change (core.py's "... Use format NdM." already conformed). Re-establishment
consumed NO repair budget (standing rule: the 2.0 red was a verified spec defect).
P5: red(2.0) -> [spec/contract predicate added] -> GREEN. Proceeding to the
first-ever P6/P7/P9.

## P6 integration (__main__ adapter) — GREEN, first-ever exercise (2026-07-05)
```
=== phase P6 gate: GREEN ===
  [PASS] T6.1 (block)  -> run_check python -m pytest -q -> 8 passed (suite still green after __main__ added)
  [FAIL] V6.1 (nonblock) -> verdicts/p6.json empty/invalid JSON (non-gating)
```
First P6 in program history: T6.1 wrote echobot/__main__.py (stdin->handle_message->stdout);
the full suite still passes. 0 nudges, no truncation. No repair needed.

## P7 acceptance (stdin) — RED, first-ever exercise (adapter never imports the core) (2026-07-05)
```
=== phase P7 gate: RED ===
  blockers: T7.1
  [FAIL] T7.1 (block)  — all 3 acceptance run_checks: `python -m echobot` exit 0 but stdout MISS
      FAIL python -m echobot  stdin "!ping\n"        expected stdout contains "pong"        -> no output
      FAIL python -m echobot  stdin "!echo hello world\n"  expected "hello world"           -> no output
      FAIL python -m echobot  stdin "!roll 2d6\n"    expected "rolled 2d6:"                  -> no output
  [PASS] V7.1 (nonblock) -> verdicts/p7.json VALID (verifier fidelity: p7 valid; p3/p5/p6 were not)
```
### Root cause: __main__ never imports handle_message + swallows the NameError
echobot/__main__.py calls `handle_message(line, random_instance)` with NO
`from echobot.core import handle_message`, and wraps the call in
`try: ... except NameError: pass`. Every line raises NameError (undefined name),
is silently swallowed -> zero stdout, exit 0. The adapter's own comment admits it:
"Assuming handle_message is defined or imported elsewhere". The P6 gate ran only
`pytest -q` (which imports echobot.core directly and never executes __main__), so
the bug was invisible until P7 drove the assembled bot over stdin. **Phase-depth
design validated: a defect the earlier gate structurally could not see surfaced at
the acceptance gate.** T6.1's description ("pass each to handle_message") never
inlined the import contract — the F6/F8 pattern (missing-contract) recurring at the
adapter. Repair per protocol: ONE `--rerun-task T6.1 --oracle P7`.

### P7 repair — RED (second red): STOP
```
=== phase P7 gate: RED (mechanical re-eval) ===
  [FAIL] T7.1 (block)  — `python -m echobot` now EXITS 1 (NameError surfaced), still stdout miss
      NameError: name 'handle_message' is not defined   (echobot/__main__.py line 16)
  [PASS] V7.1 (nonblock) -> verdicts/p7.json valid
```
Repair T6.1 (rerun, oracle=P7): tools_used=['mark_complete','write_file'], status=passed (own gate).
gemma REMOVED the `except NameError: pass` (good — the error now surfaces) and renamed the var, but
STILL did not add `from echobot.core import handle_message` (line-4 comment: "Assuming handle_message
is defined elsewhere or available in scope"). P7 reds a SECOND time -> STOP, waivers forbidden.

### Findings surfaced by the first-ever P6/P7 (see run-1 findings Part 9)
1. **Adapter missing the import contract (F6/F8 recurrence at P6).** T6.1's description
   ("pass each to handle_message") never inlines `from echobot.core import handle_message`;
   gemma writes the adapter assuming the name is in scope.
2. **T6.1's gate is too weak (F10 recurrence).** T6.1's only run_check is `pytest -q`, which
   imports echobot.core directly and NEVER executes __main__ — so T6.1 passes its own gate while
   the adapter is broken. The adapter task needs a behavioural smoke gate (drive `python -m echobot`
   over stdin), exactly as P4 got F10 smoke gates.
3. **Error-swallowing starved the repair oracle (F17).** The first __main__ wrapped the call in
   `except NameError: pass` -> exit 0, no traceback -> the P7 acceptance oracle could carry only
   "no stdout", not the NameError. The repair removed the swallow (surfacing the error) but could
   not also infer the missing import within the one-repair budget. Defensive swallowing degrades
   F7 oracle expressiveness.

### Phase gate ledger (run 2.1)
```
P3 GREEN | P4 GREEN(from 2.0) | P5 RED(2.0)->[F15 predicate]->GREEN | P6 GREEN | P7 RED->(repair T6.1)->RED -> STOP
```

**RUN 2.1 STOPPED at P7 (second red on the same gate). Reached the FURTHEST point in program
history: P3-P6 green, P7 (first-ever acceptance gate) exercised and producing genuine findings
(F16 adapter import-contract + weak gate; F17 oracle-starving error-swallow). P9 not reached; run
did not complete; no PROJECT_STATE.md. No waiver.** V7.1 verifier produced VALID JSON (first valid
verdict since V4.1) — verifier fidelity remains inconsistent across phases.

### Final workspace tree (runs_out/integration-run-2/echobot/echobot, git/pycache elided)
```
README.md
echobot/__init__.py
echobot/__main__.py        (adapter: NameError — never imports handle_message; T6.1 gate too weak to catch)
echobot/core.py            (module-level handle_message; 8/8 core tests pass)
requirements.txt
tests/test_core.py         (8 tests incl. the F15-predicate roll_malformed, all green)
verdicts/p4.json (valid) / p5.json (malformed) / p6.json (empty) / p7.json (valid)
```
PROJECT_STATE.md: NOT PRESENT (P9 not reached — run did not complete).

---

# RUN 2.2 — F16/F17 fixes: T6.1 import contract + stdin smoke gate + no-swallow clause; F17b persistence

## Pre-flight (2026-07-05) @ run-2.2 prep (suite green 1450, ruff clean)
```
ollama /api/version: {"version":"0.31.1"}; gemma4:e4b + qwen2.5:7b-instruct resident
conditions delta:
  (F16) T6.1 inline contract now names `from echobot.core import handle_message` explicitly + adds a
        behavioural smoke run_check (python -m echobot, stdin "!ping\n" -> stdout contains "pong").
  (F17) T6.1 clause: "Do not wrap the core call in defensive try/except; let errors propagate."
  (F17b) reevaluate_phase_gate's run_check captures now PERSIST to tasks.jsonl as mechanical-marked
         TaskRecords attributed to the owning task (build_mechanical_task_records; TaskRecord.mechanical
         field added, additive). So the next oracle_records_for_phase resolves post-repair reality.
ledger: P3/P4/P5/P6 green, P7 red (run-2.1 second red). core.py untouched (8/8 core tests pass).
action: conditions-defect re-establishment of P6 -> `--rerun-task T6.1 --oracle P7` (runner requires
  --oracle; consumes no budget per the standing rule — F16/F17 are a verified missing-contract/weak-gate
  defect), then --next through P7/P9.
```

## P6 re-establishment + P7 acceptance — GREEN (F16/F17 fixes landed) (2026-07-05)
`--rerun-task T6.1 --oracle P7` (no budget). T6.1 rewrote __main__.py under the
new contract; item-3 capture — the import is now present:
```
runs_out/integration-run-2/echobot/echobot/echobot/__main__.py
  1: import sys
  2: import random
  3: from echobot.core import handle_message      <- F16 fix: explicit import present
```
```
=== phase P7 gate: GREEN (mechanical re-eval) ===
  [PASS] T7.1 (block)
      ok  python -m echobot  stdin "!ping\n"        -> stdout "pong"
      ok  python -m echobot  stdin "!echo hello world\n" -> stdout "hello world"
      ok  python -m echobot  stdin "!roll 2d6\n"    -> stdout "rolled 2d6: 1+2=3"
  [PASS] V7.1 (nonblock) -> verdicts/p7.json VALID
```
World (a): adapter imports + acceptance all green, first-ever P7 pass. The no-swallow
clause (F17) + import contract (F16) resolved it; T6.1's own new stdin smoke gate
(python -m echobot !ping -> pong) means the adapter now reds on its OWN defect at P6.
### F17b VERIFIED LIVE
The mechanical P7 re-eval persisted a mechanical-marked T7.1 TaskRecord
(mechanical=True, status=passed, 3 run_check records) to tasks.jsonl — the re-eval's
captures now survive for any subsequent oracle_records_for_phase (latest-record-wins).

## P9 docs + handoff — RED first pass (T9.2 stalled on PROJECT_STATE.md) (2026-07-05)
```
=== phase P9 gate: RED ===
  blockers: T9.2
  nudge accounting: 1 fired
  [PASS] T9.1 (block)  README.md -> contains "python -m echobot" + "pytest"
  [FAIL] T9.2 (block)  PROJECT_STATE.md -> ALL 10 file_contains checks fail (8 headers + 2 gate cmds)
      tools_used=[] , iters=2, nudges=1 : TWO empty turns (tool_calls=0 content_len=0) -> no artifact written
  [PASS] V9.1 (nonblock) -> verdicts/p9.json VALID
```
T9.2 stalled: both turns empty, the one nudge did not recover, PROJECT_STATE.md never
created. The task is fully specified (8 headers inline, F6), so this is a genuine model
stall on a large structured document — NOT a conditions defect; it consumes budget.
Designated repair per standard protocol: ONE `--rerun-task T9.2 --oracle P9`.

### P9 repair — RED (second red): STOP
```
=== phase P9 gate: RED ===
  blockers: T9.2
  [FAIL] T9.2 (block)  PROJECT_STATE.md -> all 10 checks fail again
      tools_used=[], iters=2, nudges=1 : TWO empty turns AGAIN — PROJECT_STATE.md still never written
  [PASS] V9.1 (nonblock) -> verdicts/p9.json VALID
```
The repair reproduced the stall exactly (two empty turns, one ineffective nudge, no
artifact). Second P9 red -> STOP, waivers forbidden.

### F18 — model floor on large structured-document generation (the deferred measurement, answered)
gemma-e4b stalls on T9.2 (PROJECT_STATE.md: 8 mandatory verbatim section headers +
verification record + file map) — pure empty turns (tool_calls=0, content_len=0), both
the attempt and the repair, the S2 nudge not recovering. Contrast T9.1 (README, ~20
lines, 2 substrings) which PASSED first try. So the floor is specifically the LARGE,
highly-structured handoff document, not doc-writing per se. This IS the deferred run-1
measurement — "can the implementer describe its own project accurately?" — answered:
at this model size, NO; it produces no output at all for the 8-section brief. Distinct
from F14 (partial/incorrect implementation): here the model emits nothing. Fix
candidates for a run 2.3 (owner's call): decompose T9.2 into per-section tasks (the
F14 small-task mitigation applied to docs), or reduce the mandatory-section scope, or
seed a scaffolded template the model fills. None is a run-2.2 change.

### Phase gate ledger (run 2.2)
```
P3 GREEN | P4 GREEN | P5 GREEN | P6 GREEN | P7 RED(2.1)->[F16/F17 fix, re-est]->GREEN | P9 RED->(repair T9.2)->RED -> STOP
```

**RUN 2.2 STOPPED at P9 (second red on the same gate; T9.2 PROJECT_STATE.md stall, F18).
Reached the FURTHEST point in program history: P3-P7 green (first-ever P7 acceptance
pass), P9 exercised (first-ever), README green, PROJECT_STATE.md unwritten. Run did NOT
complete; no PROJECT_STATE.md to fact-check (the stalled artifact IS the fact-check
target). No waiver.** V-fidelity: V7.1/V9.1 valid JSON this run; V5.1/V6.1 not.

### Final workspace tree (runs_out/integration-run-2/echobot/echobot, git/pycache elided)
```
README.md                  (T9.1 GREEN — small doc, ~20 lines)
echobot/__init__.py
echobot/__main__.py        (imports echobot.core; python -m echobot acceptance GREEN)
echobot/core.py            (module-level handle_message; 8/8 core tests pass)
requirements.txt
tests/test_core.py         (8 tests incl. F15-predicate roll_malformed)
verdicts/p4.json (valid) p5.json (malformed) p6.json (empty) p7.json (valid) p9.json (valid)
```
PROJECT_STATE.md: NOT PRESENT (T9.2 stalled twice — F18; P9 not passed).

---

# RUN 2.3 — F18' test: output envelope 2048 -> 4096 (single variable)

## Pre-flight (2026-07-05) @ run-2.3 prep (suite green 1450, ruff clean)
```
ollama /api/version: {"version":"0.31.1"}; gemma4:e4b + qwen2.5:7b-instruct resident
conditions delta (single variable): gemma-e4b max_tokens 2048 -> 4096.
  CONFIG-PROVENANCE NOTE: the addendum named campaigns/integration-run-2.yaml, but
  run_phased does NOT read campaign.params.max_tokens — it resolves inference params
  from the CAST-bound PROFILE (profiles.yaml gemma-e4b). So the campaign field was
  INERT; the effective change was made on the gemma-e4b profile (max_tokens 4096),
  with the campaign param kept in sync as documentation. This inertness is itself a
  latent finding (config knob that silently does nothing). num_ctx unchanged (8192);
  max_tokens=num_predict is per-request, so no model reload needed.
ledger: P3/P4/P5/P6/P7 green, P9 red (run-2.2 second red, T9.2 F18/F18' stall).
action: re-establish P9 -> `--rerun-task T9.2 --oracle P9` (no budget pending world (a));
  then --next to close the run if green.
```

## P9 re-establishment at max_tokens=4096 — RED (world (b): still empty) (2026-07-05)
```
=== phase P9 gate: RED ===  blockers: T9.2
  [FAIL] T9.2 (block)  PROJECT_STATE.md -> all 10 checks fail; tools_used=[], nudges=1, artifact_capture=None
  [PASS] V9.1 (nonblock) -> verdicts/p9.json VALID
```
### Discriminator (item-3) — durations + content_len at 4096
```
turn 1:  41.0s generation -> tool_calls=0, content_len=0    (vs 23.6s at 2048)
  nudge 1/1 fired (PROJECT_STATE.md not written)
turn 2:  17.8s generation -> tool_calls=0, content_len=0
```
**F18' (output-envelope starvation) is NOT confirmed by the 4096 bump — the opposite.**
Doubling the cap did not land the artifact; turn 1 generated LONGER (41s vs 23.6s) and
still returned content_len=0. This is a long generation yielding zero PARSEABLE output,
not a fast stall and not a simple truncation the higher cap would relieve. World (b):
STOP, then a single OLLAMA_DEBUG diagnostic of the raw generation (below). No
decomposition without chat-side sign-off.

**RUN 2.3 STOPPED at P9 (world (b), F18' not confirmed). Ledger unchanged: P3-P7 green,
P9 red. Diagnostic follows as a separate step.**

## OLLAMA_DEBUG diagnostic — T9.2 raw generation (world (b), separate step) (2026-07-05)
Direct /api/chat to gemma4:e4b with T9.2's EXACT manifest (implementer allowlist:
read_file/write_file/list_directory/mark_complete) + the 8-header brief; num_ctx 8192,
num_predict 4096, temp 0, seed 42. Raw response (before Agora's <think>-stripping):
```
done_reason: stop            <- NATURAL termination, NOT "length" => F18' (truncation/envelope) FALSIFIED
eval_count: 502 tokens
message.tool_calls: [write_file(content="## Identity\n\n## Architecture & invariants\n
                     ## Capability inventory\n## Verification record\n- ... `python -m pytest -q` ...")]
message.thinking len: 1610   |   message.content len: 0
```
The tool_call content carried ALL 8 headers + both gate commands — i.e. it WOULD HAVE
PASSED the P9 gate had it landed. Raw generation TAIL (verbatim, from message.thinking):
```
 -m pytest -q`
    *   The `python -m echobot` acceptance (I will list this as a command/test).
Since no other content is specified for the sections, I will just ensure the headers
are present and the specific requirement for "## Verification record" is met.
**Drafting the Markdown Content:**
```markdown
## Identity

## Architecture & invariants
... (all 8 headers) ...
## How to run / test
```
I will use the `write_file` tool.
```
### Reframing — F18'' (reasoning-emission reliability at doc scale), NOT a model floor
gemma is CAPABLE: it drafts the exact correct document inside a ~1600-char reasoning
trace and, in the successful raw call, emits a valid structured write_file
(done_reason=stop). But the behaviour is UNRELIABLE — a second raw call (terser prompt,
same seed) produced the reasoning and NO structured tool_call (tool_calls=0),
reproducing the empty turn. Mechanism (llm_adapter.py:401-423): the adapter strips
<think> from content, reads structured msg["tool_calls"], and only runs the text-fallback
parser when the stripped content is non-empty. When gemma spends the turn REASONING and
does not emit a structured call, the stripped content is empty -> no call parsed ->
content_len=0, tool_calls=0 (exactly the real run), and the S2 nudge re-triggers the same
derailment. So the P9 blocker is NOT F18 (model can't) nor F18' (envelope) — it is a
reasoning-vs-action emission gap on the largest, most open-ended task in the flow.
NO decomposition applied (chat-side sign-off required per the addendum). No code change
this step — the diagnostic is the deliverable; the fix is a design question (options:
force/greedy tool emission, recover a drafted artifact from the thinking trace, or the
scheduled mechanical FACT-section extractor). Ledger unchanged: P3-P7 green, P9 red.
