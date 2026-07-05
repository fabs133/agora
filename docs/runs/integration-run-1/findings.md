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

---

## Part 6 — run 1.4 findings (2026-07-05)

**Run 1.4 outcome:** P5 red -> designated repair -> red -> stop, **world
(b)** as pre-registered — in a sharper form than predicted. The two prep
items (implementer-seat allowlist; write_file affordance line) both
landed, but their interaction with a latent v2.4 guard left the seat
UNABLE TO WRITE, so the repair could not act. Not a model finding; a
framework-collision finding.

**F13 — the write_file-hide overwrite guard and a seat allowlist are
mutually exclusive as written.** `_run_loop` (agent_runtime.py:408-420)
drops `write_file` from the manifest every turn when the task's output
file already has bytes (`_output_path_has_content`, condition 1). Its
purpose (v2.4) is to push the model off re-writing and onto the edit
family (add_function / edit_file_*). Run 1.4's allowlist removed that
edit family. Net manifest on the pre-existing (drifted) core.py =
{read_file, list_directory, mark_complete} — no write tool at all. gemma
read the file, found nothing to modify it with, and produced an empty
turn -> auto-mark-complete -> core.py never written -> mechanical P5 red.
The guard assumes the edit family is the fallback; the allowlist assumes
write_file is; neither is true simultaneously. **Which tool wrote
core.py: none. Did the guard misdirect: yes — structurally.** The
repair prompt's item-2 line ("Rewrite the file with write_file using
force=true") named a tool the guard had hidden — a prompt↔manifest
contradiction that made correct guidance inert.

**F13 fix (run 1.5, single variable):** make the write_file-hide guard
ALLOWLIST-AWARE. When the seat exposes no edit-family tool, do NOT hide
write_file — it is then the seat's only write affordance, and the whole
premise of the hide (redirect to edit tools) is void. Smallest form: in
the `_run_loop` per-turn filter, skip the write_file drop when the
manifest contains none of the edit-family names (or, equivalently, when
`identity.config.allowed_tools` is set and excludes them). The item-2
affordance line becomes effective the moment this lands. (Deferred, still
registered from Part 5: rejection-stall detector; add_function audit;
guard-message toolset-awareness — F13 subsumes the last of these for the
hide path.)

**Item verifications.** Allowlist mechanism (item 1): VERIFIED — T4.1
emitted no add_function/edit call; the 1.3 fixation is gone; the restricted
manifest is what exposed F13. write_file affordance line (item 2):
DELIVERED (unit-tested) but inert under the F13 collision. F10 local
smoke gate: red again on the drift, as designed. Verifier seat left
unrestricted (allowed_tools=()): confirmed.

**Exit criterion note.** Part 5 pre-committed run 1.4 as the LAST repair
iteration on this workspace, next execution = run 2.0 (clean greenfield).
F13 is a REPAIR-path bug (it bites only when re-editing an existing file);
a greenfield run 2.0 writes core.py the first time (empty output -> write_file
available) and never hits it, so 2.0 stands unblocked. But F13 must land
before ANY future repair-on-existing-file, and the accumulated-fixes claim
for 2.0 now includes an allowlisted seat whose guard interaction is only
safe post-F13. Recommendation (owner's call): apply the one-line F13 fix
and re-run the single T4.1 repair ONCE as a clean confirmation of the
end-to-end repair path (contract + authority + local gate + write_file
affordance + a manifest that actually offers write_file) before declaring
run 1.x closed and moving to 2.0. This would be the first repair cell in
program history to have every provision simultaneously in channel.

**Part 6 addendum — chat-side ruling (2026-07-05).**

**F12 narrative correction:** in run 1.3 write_file was HIDDEN by the
v2.4 guard (core.py had bytes) — gemma did not fail to fall back to
write_file; there was nothing to fall back to. Its add_function choice
was the semantically correct pick from the only family offered. F12's
tool-surface doctrine stands; the model's 1.3 conduct upgrades. Run 1's
"no guard friction" observation is likewise reinterpreted: friction was
invisible because the guard shaped the MANIFEST, and manifest shaping
does not appear in tools_used forensics. New observability rule:
manifest deltas (guard hides, allowlist filters) are logged per turn.

**F13 correction to the report:** NOT repair-path-only. Greenfield
T3.1 writes a non-empty stub core.py; T4.1 must modify it; guard hides
write_file; allowlisted seat has no edit family -> the same collision
at P4 in run 2.0. F13 is on the greenfield critical path.

**F13 fix (invariant form):** the guard may never reduce a seat to zero
file-mutation affordances — if hiding write_file would leave the
manifest without any mutation tool, the hide is skipped. Regression
test: allowlisted seat + existing output file -> manifest contains
write_file. Property test: any task with an output_path always has
>=1 mutation affordance offered.

**Exit-criterion amendment, made openly:** the 1.4 pre-commitment
("last repair iteration") targeted repair grinding against unknown
walls. 1.4 was INVALIDATED (manifest contained no write affordance —
the trial never reached the model), not failed — same category as run
1's scope-blocked P5, ratified precedent. Therefore: run 1.5 is granted
as the RE-ESTABLISHMENT of the 1.4 trial, and is FINAL for run 1.x
regardless of colour. Green -> proceed P6-P9 to completion. Red -> the
repair cell closes as model-side under full provisioning (contract +
authority + local gate + oracle + offered affordance — the first
attempt in program history with all five simultaneously in channel),
and run 1.x ends. No further fixes inside run 1.x after 1.5.

**Run 1.5 worlds:** (a) write_file(force) rewrite lands -> local smoke
green -> mechanical P5 green -> --next P6/P7/P9 -> completion +
PROJECT_STATE fact-check. (b) red with all five provisions verified
in-channel -> model-side repair-floor finding, run 1.x closed, full
report written. (c) any provision found NOT in channel post-hoc ->
invalidated trial, run 1.x closed anyway (exit rule), defect queued for
run 2.0. Budget: this one rerun. Waivers forbidden.

---

## Part 7 — run 1.5 findings — RUN 1.x CLOSED (2026-07-05)

**Run 1.5 outcome:** P5 red -> STOP, run 1.x closed per the exit
amendment. But the character of the red inverted: **world (b)** — the
repair path worked END-TO-END for the first time in program history, and
the residual is a clean, model-side implementation-completeness floor.

**F13 fix VERIFIED LIVE.** The invariant `_apply_overwrite_guard` held:
with the impl seat allowlisted (edit/AST family de-listed), the guard did
NOT hide write_file on the pre-existing drifted core.py — run.log shows
`manifest: filtered 13 tools (allowlist) task=T4.1` and NO
`manifest: hid write_file` line for T4.1. write_file was offered; gemma
read the drift, called `write_file` (turn 2), and landed a MODULE-LEVEL
`def handle_message(text: str, rng: random.Random) -> Optional[str]`.
T4.1 passed its own gate (all four postconditions, incl. the F10 ping
smoke). **The tool that wrote core.py: write_file. The F4 drift is GONE.**
The 1.4 guard×allowlist collision cannot recur; manifest shaping now
leaves evidence (the Part-6 observability rule).

**F14 — the repair floor is implementation COMPLETENESS, not perception,
tool choice, or affordance.** With all five provisions simultaneously in
channel for the first time (inline contract + F9 authority + F10 local
gate + verbatim oracle + offered write_file), gemma executed the
class->function refactor and got 4 of 8 spec behaviours right (ping, echo,
unknown_command, non_command). The 4 it missed, on a from-scratch rewrite
under a fully-NAMING oracle:
- test_help_lists_all_commands — help omits `!roll` (implemented dispatch,
  forgot the help line);
- test_roll_malformed — usage string `Usage: !roll <N> <M>...` vs the
  spec/test's `Usage: !roll NdM`;
- test_roll_deterministic — `rolled NdM: a+b=total` format mismatch;
- test_echo_preserves_spacing — internal spacing not preserved.
This is not F7 (oracle expressiveness — the oracle NAMED roll_malformed /
help): the oracle told gemma these failed and it still did not reproduce
the exact contract. The floor is capability to reconstitute a full
behavioural spec from scratch, gate-checked. The prior floors are now
retired as instrument artifacts: 1.2 no-op (no local gate), 1.3
add_function (unmeasured tool surface), 1.4 no affordance (guard
collision). 1.5 removed all three and reached the real one.

**The repair doctrine is complete for run 1.x.** The full recipe that
makes an oracle-fed repair ACT correctly: (i) the failing gate on — or a
contract naming — the repaired task (F10, [[repair-oracle-needs-local-gate]]);
(ii) the authoritative contract inline + an authority clause (F8/F9);
(iii) a verbatim, tail-preserving oracle (F7/F11); (iv) a seat held to a
measured tool surface (F12); (v) that surface actually OFFERING a write
affordance on an existing file (F13). All five landed; the repair then
does exactly what the model is capable of — here, a partial reimplementation.

**Run 1.x CLOSED.** Per the Part-6 exit amendment, 1.5 was final
regardless of colour. No further fixes inside run 1.x. Net yield of run
1.x (from a bot that still does not fully pass its own tests): F1–F14,
role-scope lint, run.log observer, order_after verifiers, mechanical-flag
ledger, inline-contract tasks, behavioural smoke gates, head+tail oracle
capture, the F9 authority clause, the seat allowlist mechanism
(AgentConfig.allowed_tools), the F13 guard invariant, manifest-delta
logging, and the model×tool-surface casting dimension. Next execution is
**run 2.0** — a clean greenfield run of the full flow under all
accumulated fixes, where T4.1 carries its contract from turn 1 and the
drift most likely never occurs. The one open behavioural question F14
raises (does gemma reconstitute the FULL contract when writing fresh,
rather than repairing?) is a run-2.0 measurement, not a run-1.x fix.

**Deferred, still registered (not built in run 1.x):** rejection-stall
detector; add_function audit (append vs upsert, `path` requirement);
verdicts/p5.json confirmation on a P5-touching run (V5.1 never re-ran in a
P4-task rerun). These carry into the run-2.0 backlog.

---

## Part 8 — run 2.0 findings, F15 doctrine, run 2.1 pre-registration (2026-07-05)

**Run 2.0:** P3 green, P4 red->green (repair landed !roll in one pass),
P5 red->repair->red->stop on ONE test. F13 verified live across a full
greenfield run (allowlisted seats kept write_file; unrestricted verifier
correctly guarded — both branches observed). F14-fresh: 6/8 first pass,
7/8 after one named-oracle repair — incremental build + per-task smoke
gates beat 1.5's whole-file 4/8, confirming the pre-registered F12xF14
mitigation.

**F15 — spec self-testability.** The blocker was a contract ambiguity,
not a defect: the spec said "malformed spec -> a usage message" with no
acceptance predicate; the tester invented literals; the implementation
answered correctly in different words. Neither is wrong against the
human spec; the gate reds on the SPEC AUTHOR'S omission — the first
planner-side finding of the program (the taxonomy's Phase-1 failure
surface, missed by its own human review). Doctrine, adopted: every
behavioural requirement carries its own acceptance predicate — exact
output, or an explicit tolerance; implicit freedom is delegated
ambiguity. Applied: the roll-malformed requirement now reads "usage
message that MUST contain 'NdM'" — gemma's live output already
conforms; only the test regenerates.

**Standing protocol rule — conditions-defect re-establishment (third
use, now named):** a trial whose conditions contained a VERIFIED defect
(framework bug, channel starvation, spec defect) re-establishes without
consuming repair budget; a trial that reached the model under valid
conditions consumes budget. Precedents: run 1 scope bug, run 1.4
affordance void, run 2.0 spec defect.

**Verifier fidelity record (non-gating, axis-2 data):** instruct as
verifier across the program: verdict "pending" (1.2) -> post_note, no
artifact (1.2') -> file written, malformed JSON (2.0). Trend: protocol
adherence improving, artifact fidelity not yet reliable. No fix; the
record IS the measurement.

**Run 2.1 pre-registration.** Conditions delta: the F15 predicate in the
spec + T5.1's inline contract (one line each); nothing else changes;
core.py untouched (already conforms). Action: conditions-defect
re-establishment of P5 — rerun T5.1; then --next through P6/P7/P9.
Worlds: (a) tester asserts the stated predicate -> pytest green ->
proceed; P6/P7 are FIRST exercises (instrument findings likely; standard
protocol, one repair each, second red stops); completion -> PROJECT_STATE
human fact-check. (b) tester deviates from a now-stated predicate ->
red; ONE repair (this time deviation is attributable — the contract
names the predicate); second red stops with a genuine tester-fidelity
finding. Waivers forbidden. Budget: fresh per gate for the never-run
phases; P5's re-establishment consumes none per the standing rule.

---

## Part 9 — run 2.1 findings (2026-07-05)

**Run 2.1:** furthest point in program history. P5 re-established GREEN
(world (a) — the tester wrote `assert result is not None and "NdM" in
result`, citing F15; the named acceptance predicate resolved the 2.0
ambiguity with zero implementation change). **P6 GREEN first-ever** (T6.1
wrote the __main__ adapter; suite still 8/8). **P7 RED first-ever ->
repair -> RED -> STOP.** P9 not reached; no PROJECT_STATE.md.

**F15 validated.** Naming the acceptance predicate ("usage message MUST
contain 'NdM'") closed the 2.0 blocker at the re-establishment: the tester
conformed to the stated predicate verbatim. Spec self-testability works —
the ambiguity was the whole defect. Re-establishment consumed no repair
budget (standing rule; the 2.0 red was a verified spec defect).

**F16 — the adapter task repeats the missing-contract + weak-gate pattern
at a new phase (F6/F8 x F10, recurrence).** P7's stdin acceptance is the
first thing to actually EXECUTE __main__; it caught a bug two green gates
could not. Two coupled causes: (i) T6.1's description says "pass each to
handle_message" but never inlines `from echobot.core import
handle_message`, so gemma wrote the adapter assuming the name was in scope
(the F6/F8 missing-contract pattern, now implementer-side at the adapter);
(ii) T6.1's ONLY gate is `pytest -q`, which imports echobot.core directly
and never runs __main__ — so T6.1 passes its own gate while the adapter is
broken (the F10 weak-gate pattern: a task's gate must exercise the
contract that task owns). Fix (run-2.2 flow delta): T6.1 gains the import
contract inline AND a behavioural smoke run_check that drives `python -m
echobot` over stdin (`!ping` -> `pong`), so the adapter's own gate reds on
its own defect — the direct analogue of the P4 F10 smoke gates.

**F17 — defensive error-swallowing starves the repair oracle.** The first
__main__ wrapped the call in `try: ... except NameError: pass`. The
NameError from the missing import was swallowed, so `python -m echobot`
exited 0 with empty stdout — and the P7 acceptance oracle could carry only
"stdout did not contain 'pong'", NOT the NameError that explains it. The
one repair (rerun T6.1 --oracle P7) correctly removed the swallow
(surfacing the error, exit 1 now) but could not also infer the missing
import from a symptom-only oracle within budget. Doctrine: an artifact
that swallows its own errors degrades F7 oracle expressiveness — the
oracle can only teach what the failure EXPOSES. This pairs with F16(ii):
a behavioural smoke gate on T6.1 would have surfaced the NameError at P6
(exit 1), before the acceptance phase, with the error un-swallowed and the
repair budget intact.

**Verifier fidelity record (continued).** V7.1 produced VALID JSON (first
since V4.1); V5.1/V6.1 did not (malformed / empty). Instruct's verdict
fidelity remains phase-inconsistent — the record is the measurement, no
fix.

**Nudge / truncation.** P3 1 nudge (S2); P5-re-est / P6 / P7 tasks 0. No
truncation events (all outputs under the F11 head+tail bound). Infra held.

**Disposition — run 2.2 pre-registration.** The stop is a clean instrument
result: the first-ever adapter+acceptance exercise found a real,
previously-unreachable class (F16/F17), exactly the phase-depth design's
purpose. Conditions delta (flow, small): T6.1 gains (i) the inline import
contract (`from echobot.core import handle_message`) and (ii) a
behavioural smoke run_check (`python -m echobot` stdin `!ping\n` ->
stdout contains `pong`) — the F16 fix. Action: conditions-defect
re-establishment of P6 (rerun T6.1 — a verified missing-contract/weak-gate
defect, no budget) then --next through P7/P9. Worlds: (a) adapter imports
+ smoke green -> P6 green -> P7 acceptance green -> P9 -> completion +
PROJECT_STATE human fact-check; (b) adapter still wrong under the inline
import + smoke oracle -> one repair -> second red stops with a genuine
model-side adapter finding. Waivers forbidden. The deferred PROJECT_STATE
measurement still waits on a completed run; P9 remains the last
never-reached phase.
