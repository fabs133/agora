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
