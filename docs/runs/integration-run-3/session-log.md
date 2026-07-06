# Integration run 3 — echobot BROWNFIELD probe — session log

*Verbatim execution log. Binding spec/pre-registration:
docs/integration/run-3-brownfield-spec.md. Baseline: tag echobot-v1 (PROJECT_STATE.md
v1.1, fact-check PASS). Branch feat/integration-run-3 (cut off feat/integration-run-2).
Campaign: campaigns/integration-run-3.yaml. Flow:
flows/integration-run-3-brownfield.flow.yaml. Provenance (untracked):
runs_out/integration-run-3/.*

## Provenance note — starting state
Copied the echobot-v1 workspace tree (from tag echobot-v1 / the run-2 artifact at
runs_out/integration-run-2/echobot/echobot) to runs_out/integration-run-3/echobot/echobot
(echobot/ package, tests/, prose/, README.md, requirements.txt, PROJECT_STATE.md v1.1;
excluded .git/__pycache__/verdicts). git-init'd the workspace + committed the baseline
(151a901). Fresh ledger (runs_out/integration-run-3/, no run-2 bleed). Cast p40-24gb
unchanged; implementer allowlist stands (F13); tester owns tests/.

## Runner delta — --phase0 <artifact> (item 1)
run_phased.py gains `--phase0 <PROJECT_STATE.md>`: parse_verification_run_checks (F20
form) -> execute each run_check in the project dir -> write a P0 PhaseGateRecord
(mechanical-marked) -> print the standard gate report. Unit test: a fixture artifact
with one passing + one failing check -> the P0 record reflects BOTH (gate red, both
run_check records captured). --status surfaces the ledger-only P0 ahead of the task phases.

## P0 — re-validation + RED-TEAM FIRST (before any task) — PASS (2026-07-06)
The brief's protective gate is only trusted after it is seen FAILING then passing.
```
STALE : core.py L47  "Use format NdM."  ->  "Use format N-d-M."  (removed the "NdM" token)
--phase0 -> RED : run_check[0] python -m pytest -q FAILED
   test_roll_malformed: assert 'NdM' in 'Malformed roll specification: invalid. Use format N-d-M.'  -> AssertionError
   (run_checks [1..3] python -m echobot ping/echo/roll still ok)
RESTORE: git checkout -- echobot/core.py
--phase0 -> GREEN : pytest -q 8 passed; ping->pong, echo->hello world, roll->rolled 2d6: ...
```
The F20 verification record is a LIVE protective gate: a single staled source byte reds
P0 before task one; restoration greens it. World (c) NOT triggered — the run proceeds.
Ledger P0 rows: RED (staled) -> GREEN (restored).

## P4 — extend core (!flip, !choose) — GREEN, first try (2026-07-06)
```
=== phase P4 gate: GREEN ===
  [PASS] T4.1 (block)  — flip + choose added; all smokes green
      regression: assert handle_message('!ping', Random(0))=='pong' ; 'rolled 2d6:' in !roll  -> ok
      new (F15):  handle_message('!flip', Random(0)) in (heads,tails) ; '!choose a b c' in (a,b,c) ;
                  'choose' in handle_message('!choose') ; 'flip'&'choose' in handle_message('!help')  -> ok
```
### HEADLINE MEASUREMENT (brief-as-index navigation) — AFFIRMATIVE (run.log, quoted)
```
turn 1  read_file  PROJECT_STATE.md       <- read the BRIEF FIRST (## Identity, ## Architecture & invariants...)
turn 2  read_file  echobot/core.py        <- then the TARGET file (located via the brief's ## File map)
turn 3  write_file echobot/core.py        <- extend (3041 bytes); core signature UNCHANGED (frozen)
turn 4  mark_complete
```
The implementer RETURNED to a completed project and navigated via the handoff brief before
editing — read PROJECT_STATE.md, then the mapped target file, then extended it without
reading the old spec and without touching the frozen signature. The core question the
casting/handoff architecture exists to answer is answered YES on the first task. Regression
smokes (ping/roll) still green — no breakage.

## P5 — extend tests — RED first pass (regression 8/8 green; 4 new tests NameError) (2026-07-06)
```
=== phase P5 gate: RED ===  blockers: T5.1
  FAIL pytest -q -> 4 failed, 8 passed
    test_flip_deterministic / test_choose_no_args / test_help_lists_new_commands: NameError: name 'echobot'
    test_choose: UnboundLocalError
```
The OLD 8 tests PASS — the regression suite is intact, free of charge (spec P5'). The 4 NEW
tests reference `echobot` without importing it (the module, not `from echobot.core import ...`).
Nameable defect (F7) -> ONE repair: --rerun-task T5.1 --oracle P5.

### P5 repair -> RED again (2nd red): but a CONDITIONS DEFECT (F22), not a model floor
Repair (--rerun-task T5.1 --oracle P5): still red (7 failed) — the oracle named the NameError,
the tester fixed the import but kept the FABRICATED API and rewrote the file, breaking a
regression test (test_non_command_returns_none). run.log — the tester NEVER navigated:
```
attempt 1: edit_file_append tests/test_core.py (4 tests calling echobot.core.execute_command(text, random=...)) -> mark_complete
repair:    write_file force tests/test_core.py ("from echobot import core # Assuming...") -> still execute_command
```
No read_file of PROJECT_STATE.md or core.py in either attempt. The tester INVENTED
`execute_command(text, random=...)` — the real API is `handle_message(text, rng)`.
**F22 — the navigation/map-pointer discipline must cover EVERY editing seat, not just the
implementer.** The brownfield flow carried the map-pointer + frozen signature to the IMPL
tasks (T4.1/T6.1 — which navigated correctly) but NOT to the TESTER tasks (T5.1/T6.2). With
no pointer and no inline API, the tester fabricated — run-1's F6/F8 spec-channel starvation,
recurring TESTER-side. The model is exonerated (run-1 doctrine); this is a VERIFIED CONDITIONS
DEFECT. Per the standing rule, fix the condition + re-establish (no budget): add the map-pointer
line + the real `handle_message(text, rng)` signature to T5.1 and T6.2. (The headline navigation
measurement, by its ABSENCE in the tester seat, produced the fabrication — a clean instrument result.)

### P5 re-established — GREEN (conditions-defect fix; tester now navigates) (2026-07-06)
With the map-pointer + inline `handle_message(text, rng)` API in T5.1, the tester read the brief
and called the REAL function. Full suite **12 passed** (8 regression + 4 new). P5:
red(fabricated API) -> red(repair, condition still defective) -> [F22 fix: pointer+API] -> GREEN.
The re-establishment consumed NO repair budget (verified conditions defect, standing rule).

## P6 — Discord adapter — RED first pass (adapter over-reached the event contract) (2026-07-06)
```
=== phase P6 gate: RED ===  blockers: T6.2
  [PASS] T6.1 (block)  import echobot.discord_adapter -> ok
  [FAIL] T6.2 (block)  pytest -q -> 1 failed, 13 passed
    test_adapter_maps_ping: AttributeError: 'FakeEvent' object has no attribute 'channel'
      (discord_adapter.py:47  gateway.send(event.channel, response))
```
The DEFECT is the adapter (T6.1), not the test: the delta spec's event interface is `.content`
ONLY; run_adapter over-reached by requiring `event.channel`. The FakeGateway test (T6.2) is
spec-faithful. Regression suite still green (13 passed incl. the 12 core tests). Nameable defect
(F7) -> ONE repair on the adapter: --rerun-task T6.1 --oracle P6.

### P6 repair -> adapter still fails (channel ambiguity) + a runner false-green (F23)
The T6.1 repair rewrote the adapter (event.channel -> event.channel_id + `if channel is not None: send`)
— fixing the crash but now SKIPPING the send when the event has no channel (the spec-faithful content-only
event) -> test_adapter_maps_ping still fails (nothing sent). Two problems surfaced:
- **Root cause = spec/task under-specification (conditions defect, F15/F6 class):** the delta spec's
  `send(channel, text)` needs a channel, but events only guarantee `.content`. Where the channel comes from
  was never specified; gemma guessed the event twice. Fix the condition: T6.1 now states the channel is
  best-effort (getattr(event,'channel',None)) and the non-None response must ALWAYS be sent.
- **F23 (runner finding, backlog):** a SAME-PHASE repair of a task (T6.1) that is NOT the gate blocker (T6.2)
  evaluates only the reran task, so a broken adapter recorded a FALSE P6 green (T6.1's import passes; T6.2's
  pytest — the real blocker — was never re-run). Same-phase repair needs a full-phase mechanical re-eval,
  like the cross-phase path. For run 3 I re-establish honestly: fix the adapter, then re-run T6.2's pytest gate.

### P6 re-established — GREEN (conditions-defect fix: channel contract) (2026-07-06)
With T6.1 clarified (channel best-effort getattr(event,'channel',None); ALWAYS send the non-None
response), the adapter re-run made pytest -q **14 passed** (12 core + 2 adapter contract tests). Then
--rerun-task T6.2 --oracle P6 recorded the HONEST P6 gate (T6.2's pytest green). P6: red(adapter
crash) -> red(repair, channel still guessed) -> [conditions-defect fix] -> GREEN. import
echobot.discord_adapter ok; core UNTOUCHED. Regression suite intact throughout.
(Executor error, recorded: one stray --rerun-task ran against the run-2 campaign; the closed run-2
workspace was reset --hard to its echobot-v1 state fa4d6e8 (8/8), a stray P6 ledger line left as
provenance. Run 3's baseline was copied before this — run 3 unaffected.)
