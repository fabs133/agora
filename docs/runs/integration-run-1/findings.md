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

---

## Part 3 — run 1.1 findings and run 1.2 pre-registration (2026-07-05)

**Run 1.1 outcome:** P5 red -> repair -> red -> stop. Both run-1 fixes
verified live (role-scope: 0 rejections, write landed turn 1;
observer: tool results in run.log). The P5 red was an UNREGISTERED
world (c).

**F6 — world (c): specification-channel starvation.** The tester's
delivered context was its instructions + eight test names + the phrase
"from the spec" — with the spec never inlined and unreadable from the
workspace. Zero read attempts; turn-1 write opening "# Assuming the
module..."; a fabricated run_command() API; tests testing their own
mocks. The model is exonerated: it was asked to transcribe a document
it was never shown. Root asymmetry: T4.x carried their spec sections
inline; T5.1 carried names only. The plan-lint verified name fidelity
and never asked "does the task carry or point to the contract it
cites" — that question becomes a lint rule (F6-L): any task description
referencing an external document must inline the needed content or name
a workspace-readable path.

**F7 — repair quality is bounded by oracle expressiveness.** The
oracle-fed repair fixed exactly the failures pytest NAMED (2 failed ->
1 failed) and none it could not name (mock-only structure, zero
echobot imports). Consequence: postconditions are the vocabulary of
future repair oracles — a gate that cannot name a defect produces an
oracle that cannot teach its fix. T5.1 gains
file_contains "from echobot.core import handle_message".

**F5 (second recurrence) — verifiers gated behind failing tasks.**
V5.1 would plausibly have named the mock-only structure; it never ran.
Fix now: order_after (ordering-only) semantics for verifier tasks;
verifiers run unconditionally at their phase.

**Protocol ratification:** the executor's reading was correct — the
fresh P5 execution under fixed conditions re-established the phase the
run-1 framework bug invalidated; the repair budget applied to the
subsequent red. Ledger #5/#6 stand as attempt/repair.

**Run 1.2 pre-registration.** Conditions delta: T5.1 carries the
functional contract inline + the import postcondition; T9.2 carries the
template headers inline; verifiers order_after; mock test file removed
from the workspace (defective artifact of an invalidated attempt);
drifted core.py REMAINS (still the F4 experiment). Worlds:
- (a) spec-faithful tests -> pytest red on the drift (TypeError) ->
  designated cross-phase repair --rerun-task T4.1 --oracle P5 ->
  prediction: class->function refactor, mechanical re-eval green. The
  original experiment, third attempt at running it.
- (b) tester mocks again -> the import gate reds and NAMES it; the
  branch-2 repair (T5.1, oracle now containing the naming) tests F7
  directly: does a nameable structural defect become repairable?
- (c) anything else -> stop, interpret chat-side.
Budget: one repair per gate, second red stops. Waivers forbidden.
V5.1 verdict is expected to exist this run regardless of gate colour.

---

## Part 4 — run 1.2 findings and run 1.3 pre-registration (2026-07-05)

**Run 1.2 outcome:** world (a) reached — the F4 drift experiment finally
ran. Spec-faithful, importing tests; pytest red on the drift; designated
cross-phase repair (--rerun-task T4.1 --oracle P5) produced a READ-ONLY
NO-OP; second red; stop. World-(a) prediction FALSIFIED as registered.
All five 1.2 prep fixes verified live (F5/F6/F7, observer, mechanical
flag); first truncation event observed and correctly flagged.

**F8 — the drift and the failed repair share one root cause: the
implementer never had the contract.** T4.1's description names command
behaviours but never the signature `handle_message(text, rng) -> str |
None` nor "pure module-level function". The class-shaped core was not
drift FROM a contract — it was invention in the ABSENCE of one. F6
(spec-channel starvation), third occurrence, implementer-side. The
model is substantially exonerated again, with a recorded residual: a
stronger inference from the traceback call sites was possible; no
capability-floor claim is available while the channel is still hungry.

**F9 — repair inherits the original task's context.** The repair prompt
= original description + oracle. A repair of a context-starved task
re-runs the starvation: gemma re-read a description that never
specified a function, found core.py consistent with it, and no-oped.
Repair doctrine addition: a repair prompt must carry (i) the oracle,
(ii) the authoritative contract the oracle enforces, (iii) an explicit
authority clause ("the tests/spec are authoritative; modify YOUR
artifact").

**F10 — local gates must red on local contract violations (executor's
finding, adopted; plan-lint doctrine revised).** The plan-lint had
explicitly accepted P4's weak gate ("behaviour verified at P5"); run
1.2 falsifies that acceptance: cross-phase repair against a
locally-green gate no-ops. Every task's gate must be able to red on
violations of the contract that task OWNS. P4 gains a behavioural
smoke run_check.

**F11 — head-only truncation endangers the repair channel.**
run_check keeps the FIRST 4 KB; pytest's most diagnostic lines (tail
summary) are the first casualties. Fix: head+tail split capture with a
marked gap.

**Ratifications:** the runner extension (rerun executes order_after
verifiers) — accepted, natural completion of F5. The fresh-P5-via-rerun
protocol reading — already ratified in Part 3, stands.

**Verifier tool-fidelity (from 1.2):** V5.1 ran (F5 fix works) but
emitted via post_note, producing no verdicts/p5.json. V-task
descriptions gain an explicit "create <path> with write_file"
instruction. Calibration capture, not gating.

**Run 1.3 pre-registration.** Conditions delta: T4.1/T4.2 descriptions
carry the exact contract inline (signature, str|None return, pure
module-level function, rng injected — copied from the spec); P4 gains
run_check `python -c "import random; from echobot.core import
handle_message; assert handle_message('!ping', random.Random(0)) ==
'pong'"`; repair template gains the F9 authority+contract clause;
run_check capture becomes head+tail; V-task write_file instruction.
Workspace untouched (spec-faithful tests REMAIN; drifted core.py
REMAINS — still the experiment). Ledger note: P4's historical green
records stand; T4.1's rerun is evaluated against the NEW postconditions
at execution time — the no-op is now structurally incapable of going
green.

Designated action: ONE `--rerun-task T4.1 --oracle P5`. Worlds:
- (a) class->function refactor lands; T4.1's own smoke gate green;
  mechanical P5 re-eval green; --next proceeds P6/P7/P9. Success shape:
  run completes, PROJECT_STATE.md produced, human fact-check follows.
- (b) T4.1 edits but fails its own smoke gate -> in-task loopback data;
  green-after-loopback still counts as (a) for the run.
- (c) red again with contract + authority + local gate + oracle all in
  channel -> STOP. Only now would a model-side finding be claimable:
  gemma cannot execute a contract-driven refactor under full
  provisioning. That claim requires this run to earn it.
Budget: the one designated rerun; second P5 red stops. Waivers
forbidden. P6/P7/P9 watchlist unchanged (adapter spec inline, T9.2
headers inline, acceptance stdin checks).

---

## Part 5 — run 1.3 findings (2026-07-05)

**Run 1.3 outcome:** P5 red -> designated repair -> red -> stop, world (c).
But world (c) resolved OPPOSITE to the pre-registration's pessimistic
reading. All five 1.2/1.3 provisioning fixes were verified live and F9/F10
did their jobs; the run did NOT earn a model-capability finding. It earned
a tool-affordance one.

**F9/F10 confirmed effective (the provisioning worked).** The inline
contract (item 1) + authority clause (F9) flipped the implementer out of
the run-1.2 read-only no-op: T4.1 went from `tools_used=['read_file']`
(1.2, zero edits) to an 8-turn active repair (1.3) that AUTHORED THE EXACT
CORRECT SIGNATURE — `def handle_message(text: str, rng: random.Random) ->
str | None` — every turn. The F10 local smoke gate red on the local
contract violation (`assert handle_message('!ping', Random(0)) == 'pong'`
-> exit 1), giving the model the local defect signal
[[repair-oracle-needs-local-gate]] said was the missing ingredient. Both
levers are validated: the model perceived the defect as its own and
produced the fix.

**F12 — the repair wall moved from perception to tool affordance.** gemma
could not LAND the correct artifact because it drove it through
`add_function`, the wrong tool twice over: (i) it requires a `path`
argument gemma does not reliably emit (turns 1/2/6/7 rejected `missing
required argument(s): path` — the axis-1 tool-call-fidelity failure mode
resurfacing on a tool that is NOT gemma's proven write_file), and (ii)
`add_function` appends exactly one bare function and rejects code carrying
imports (turns 3/4/5 rejected `function code must contain exactly one
function`) — it structurally cannot REPLACE an existing drifted
`handle_message`. Across 8 identical-shaped rejections the model never
fell back to write_file / edit_file_replace, and the final empty turn
auto-completed with core.py never written. The stop is real (second P5
red, budget spent) but the diagnosis is: *under full provisioning gemma
authored the refactor; the implementer's edit-an-existing-file affordance
defeated it.* No capability-floor claim is available.

**F5/write_file (item 5) confirmed for the verifier seat.** V4.1 produced
verdicts/p4.json via write_file (`tools_used=['mark_complete',
'write_file']`) — the first verifier ARTIFACT the program has emitted
(verifiers previously post_note'd, producing nothing). V5.1 was not
re-run in a P4-task rerun, so verdicts/p5.json remains absent; the fix is
confirmed via V4.1 rather than V5.1 this run.

**Chat-side decision (owner's, not pre-registerable as a lever).** The
next run's condition delta is a tool-affordance fix, not a prompt/gate
fix. Candidates: (a) route implementer edits of an EXISTING file through
write_file/edit_file_replace and de-list or repair `add_function`'s
affordance (append-only + required `path`); (b) name the target tool and
its `path` in the repair prompt; (c) treat a repeated identical
tool-schema rejection as a stall the nudge/redirect acts on (the S2 nudge
did not fire — the rejections were non-empty turns, not empty ones, so the
empty-turn trigger stayed false — the [[repair-oracle-needs-local-gate]]
read-only-no-op corollary now extends to "rejected-write no-op"). Until
that is chosen, P5 stands red and the F4 drift is intact.

**Part 5 decision + run 1.4 pre-registration (chat-side, 2026-07-05).**

**F12 doctrine yield — tool surface is part of the evidence key.**
gemma's 9/9 casting evidence was earned on the probe's tool surface
({read_file, write_file(+force), list_directory, mark_complete}); the
AST/edit-tool family was never measured for any model. Casting the
implementer onto add_function was casting onto UNMEASURED surface — the
capability matrix gains a dimension (model x tool-surface), and Stage-3
benchmark batteries must sweep tool families. Recorded as a
roles/casting amendment: a binding's evidence must cover the tool
surface the seat exposes.

**Run 1.4 — affordance provisioning (one package, one variable):**
(i) the implementer seat in this flow gets a tool ALLOWLIST = its
measured surface (read_file, write_file, list_directory, mark_complete,
run-adjacent read-only tools as already exposed); the AST/edit family is
de-listed for this seat pending measurement. (ii) The repair prompt's
authority section gains one affordance line: "Rewrite the file with
write_file using force=true — the file exists and must be replaced."
Deferred, each with its own future registration: rejection-stall
detector (S2 extension for non-empty rejected-turn loops); add_function
audit (terse self-validation vs schema-echoing corrective rendering;
upsert-vs-append behaviour); guard-message toolset-awareness.

Designated action: ONE --rerun-task T4.1 --oracle P5 under (i)+(ii).
Worlds: (a) write_file(force) rewrite lands -> local smoke green ->
mechanical P5 green -> --next through P6/P7/P9, completion path.
(b) overwrite-guard interaction stall (its error text recommends edit
tools this seat no longer has) -> red -> STOP; guard-message
toolset-awareness becomes the single 1.5 fix. (c) other -> stop,
chat-side. Budget: one rerun; second P5 red stops; waivers forbidden.

**Exit criterion (pre-committed):** run 1.4 is the LAST repair iteration
on this workspace instance. Whatever its colour, the next execution is
run 2.0 — a CLEAN greenfield run of the full flow under ALL accumulated
fixes (contract-inline tasks, smoke gates, spec channels, order_after
verifiers, allowlisted seat), where the P4 drift most likely never
occurs because T4.1 now carries its contract from turn 1. Run 1.x has
already paid: five permanent framework upgrades and one casting-doctrine
dimension from a bot that does not exist yet. Run 2.0 is where it gets
to exist.
