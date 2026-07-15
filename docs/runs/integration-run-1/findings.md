# Integration run 1 — findings and run 1.1 pre-registration

*Findings written 2026-07-03 after forensic analysis of the stopped run
(session log b117471, provenance runs_out/integration-run-1/). Run 1.1
pre-registration recorded BEFORE its execution, per program practice.*

> **Hash banner (2026-07).** Commit hashes cited **anywhere in this file's
> historical parts predate the 2026-07 history rewrite** (a `git filter-repo`
> secret scrub that removed the tracked `workspace/` archive and relativized
> author paths — it rewrote **every** hash). Those citations are dead as written
> and are deliberately **left unrewritten**: they are the record as it was
> authored. To resolve one, use the map:
> `grep ^<old-hash> docs/history/commit-map.txt`. **Tags survived the rewrite
> and are the durable anchors** — `echobot-v1` (`957be3f4`), `echobot-v2`
> (`15edd7c9`), `lifecycle-baseline-1`. Living documents (README, SETUP,
> OLLAMA.md, arc, design) were remapped to post-rewrite hashes; this findings
> file was not.

> **Scope note.** Parts 1–17 of this file cover the whole integration
> program — runs 1.x (repair doctrine), 2.x (greenfield echobot → v1.1,
> tag `echobot-v1`), 3 (brownfield → v2.1, tag `echobot-v2`), and the
> 2026-07-15 lifecycle baseline (first clean single-session P3→P9, tag
> `lifecycle-baseline-1`; session log
> `docs/runs/lifecycle-baseline/session-log.md`). Findings are numbered
> F1–F26. Canonical index / narrative: `docs/arc/arc-outline.md`.

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

**Part 9 addendum — chat-side ruling (2026-07-05).**

**Answer to the repair question:** the model read the oracle; the oracle
was contentless. F17 (artifact swallowed its own NameError) meant
attempt-1's captures said only "exit 0, empty stdout". The repair's
removal of the swallow was the most oracle-improving move available and
CREATED the informative failure — which then never persisted:

**F17b — mechanical re-evals drop their run_check captures.**
reevaluate_phase_gate runs predicates over the workspace but appends no
task records; the post-repair NameError exists only in the printed gate
report. oracle_records_for_phase would therefore resolve the STALE
pre-repair records for any subsequent P7 repair. Fix: mechanical
re-evals append their run_check records to tasks.jsonl (mechanical-
marked, attributed to the owning task) so latest-record-wins reflects
post-repair reality.

**Run 2.2 (greenlit, augmenting Part 9's pre-registration):** T6.1 gains
(i) the inline import contract, (ii) the F10-analogue smoke gate
(python -m echobot, stdin "!ping\n" -> stdout contains "pong"),
(iii) one F17 clause: "Do not wrap the core call in defensive
try/except; let errors propagate." Plus the F17b persistence fix.
Re-establishment of P6 under the conditions-defect rule (F16 = channel
starvation class), then --next P7/P9. Worlds: (a) adapter imports and
passes its smoke -> P7 acceptance green -> P9 -> PROJECT_STATE.md ->
the deferred human fact-check. (b) P7 reds with a now-informative,
now-persisted oracle -> ONE repair (first repair in program history fed
by a post-repair-quality oracle); second red stops. Waivers forbidden.

---

## Part 10 — run 2.2 findings (2026-07-05)

**Run 2.2:** the deepest run in program history. **P6 re-established, P7
GREEN first-ever** (F16/F17 fixes landed), **P9 RED -> repair -> RED ->
STOP** on T9.2. World (a) for P7; a new model floor (F18) at P9.

**F16/F17 VALIDATED (world (a)).** T6.1, given the explicit inline import
contract + the no-swallow clause, wrote `from echobot.core import
handle_message` (line 3 of __main__.py) and the assembled bot passed all
three P7 stdin acceptance checks first try (`pong`, `hello world`,
`rolled 2d6: 1+2=3`). The F10-analogue smoke gate on T6.1 (`python -m
echobot` stdin `!ping` -> `pong`) means the adapter now reds on its own
defect at P6, not two phases downstream. The re-establishment consumed no
budget (F16/F17 were a verified missing-contract/weak-gate defect).

**F17b VERIFIED LIVE.** The mechanical P7 re-eval persisted a
mechanical-marked T7.1 TaskRecord (mechanical=True, 3 run_check records)
to tasks.jsonl — the re-eval's captures now survive for any subsequent
oracle_records_for_phase (latest-record-wins), closing the stale-oracle
hole. (Here the re-eval was GREEN so no repair keyed off it, but the
persistence is the durable fix.)

**F18 — model floor on large structured-document generation (the deferred
run-1 measurement, ANSWERED).** T9.2 (PROJECT_STATE.md: eight mandatory
verbatim section headers + a verification record + a file map) stalled
into pure empty turns (tool_calls=0, content_len=0) on BOTH the attempt
and the repair — the S2 nudge did not recover, no artifact was ever
written. T9.1 (README, ~20 lines, two substrings) passed first try, so the
floor is the LARGE, highly-structured document specifically, not
doc-writing. This directly answers the amendment's deferred question —
"can the implementer describe its own project accurately?": at gemma-e4b's
size, it produces NOTHING for the 8-section handoff brief. F18 is distinct
from F14 (partial/incorrect output): F14 is a wrong answer, F18 is no
answer — a generation stall on document scale/structure. The one-repair
budget was spent reproducing the stall exactly. Fix candidates (run 2.3,
owner's call): decompose T9.2 into per-section tasks (the F14 small-task
mitigation applied to docs — build the handoff incrementally, as the core
was built), reduce the mandatory-section scope, or seed a scaffolded
template the model only fills. None is a run-2.2 change.

**Verifier fidelity (continued).** V7.1 and V9.1 produced VALID JSON this
run; V5.1 (malformed) and V6.1 (empty) did not. Still phase-inconsistent;
the record is the measurement.

**Nudge / truncation.** P9 T9.2: 1 nudge each attempt (ineffective against
the stall); all other 2.2 tasks 0. No truncation events.

**Disposition.** Run 2.2 is the program's high-water mark: seven of eight
phases green (P3-P7 + the P9 README), the first-ever end-to-end assembled
bot passing stdin acceptance, and the last-phase measurement answered.
Every framework finding in the F1-F17b arc is now closed or verified live;
the sole remaining blocker, F18, is a MODEL floor (document-scale
generation), not a framework or spec defect — the same category the
program was built to isolate. PROJECT_STATE.md, the deferred human
fact-check target, does not exist because producing it is exactly the
capability the run measured and found absent. **Run 2.3 (pre-registration
sketch, owner's call):** decompose the P9 handoff into small per-section
tasks and re-establish P9 (F18 = a task-granularity defect, analogous to
F14's incremental-build mitigation); on completion the (now
incrementally-built) PROJECT_STATE.md finally reaches the human
fact-check. Standard protocol; the P3-P7 gates stand as a regression
suite. Waivers forbidden.

**Part 10 addendum — chat-side ruling (2026-07-05).**

**F18 provisionally reclassified F18' — output-envelope starvation.**
Forensic basis: T9.2's empty turns took 23.6s and 35.7s of generation
(run.log timestamps) — the truncation-death signature, not a fast
stall. max_tokens=2048 is inherited unchanged from axis-1 v1 (sized for
3-line probe artifacts) and a single write_file call carrying the full
8-section PROJECT_STATE.md plausibly exceeds it; a mid-JSON cut yields
zero parseable tool calls -> empty turn, nudge-immune. README fit the
envelope; the handoff doc did not. Confirmation pending run 2.3.

**Run 2.3 (single variable):** campaign max_tokens 2048 -> 4096; only
P9 executes (P3-P7 stand as the regression suite). Worlds: (a) T9.2
lands -> F18' confirmed; conditions-defect rule applies retroactively;
PROJECT_STATE.md to the human fact-check; doctrine adopted: OUTPUT
ENVELOPE SCALES WITH ARTIFACT CLASS (per-task-kind budget, matrix-
derivable). (b) still empty at 4096 -> ONE OLLAMA_DEBUG diagnostic
capture of the raw generation (malformed-at-scale vs true stall), then
run 2.4 = decomposed per-section tasks (the F14 lesson, held in
reserve to preserve attribution). (c) partial artifact -> standard
protocol; the oracle finally has content. Waivers forbidden.

**Scheduled regardless of 2.3's colour:** the agora-handoff mechanical
extractor graduates from deferred to justified — run 2's evidence:
FACT sections (file map, signatures, verification record) are
mechanically derivable and should not be model-emitted (hallucination
risk even within envelope); models write PROSE sections at README
scale. Build after run 2 closes; required quality bar for the
brownfield probe's phase-0 anyway.

---

## Part 11 — run 2.3 findings (2026-07-05)

**Run 2.3:** single variable (gemma-e4b output envelope 2048 -> 4096).
P9 re-established -> RED (world (b)); STOP; one OLLAMA_DEBUG diagnostic.
Outcome: **F18' FALSIFIED and F18 reclassified — the P9 blocker is neither
a model floor nor envelope starvation, but a reasoning-vs-action emission
gap (F18'').**

**Config-provenance finding (F19).** The addendum's "campaign max_tokens
2048 -> 4096" could not have worked as written: ``run_phased`` never reads
``campaign.params`` — inference params resolve from the CAST-bound PROFILE
(``profiles.yaml`` gemma-e4b, max_tokens 2048). The campaign field is a
silent no-op. The effective change was made on the profile instead (2048
-> 4096), campaign param synced as documentation. A config knob that
silently does nothing is a live trap; the campaign ``params`` block should
either be wired through or removed. (Recorded; not fixed this run.)

**F18' (envelope starvation) FALSIFIED.** At 4096 the stall persisted; the
discriminator killed the truncation hypothesis: turn 1 generated for 41.0s
(vs 23.6s at 2048) and still returned content_len=0 — a LONGER generation
yielding zero parseable output, the opposite of a truncation the higher
cap would relieve.

**F18'' — reasoning-vs-action emission gap at doc scale (the real
blocker; a FRAMEWORK/reliability finding, not a model floor).** The
OLLAMA_DEBUG capture (direct /api/chat, T9.2's exact manifest + brief,
num_predict 4096, seed 42) is decisive: gemma returns **done_reason=stop**
(natural termination, not "length"), a **1610-char ``thinking`` trace that
fully drafts the correct document** (all eight headers + both gate
commands — would have PASSED the gate), and — in the successful call — a
**valid structured ``write_file`` tool call** carrying that content. So the
model is CAPABLE. But it is UNRELIABLE at this scale: a second call
(terser prompt, same seed) emitted the reasoning and NO structured call.
Mechanism (``llm_adapter.py`` ~401-423): the adapter strips ``<think>``
from content, reads structured ``msg["tool_calls"]``, and runs the
text-fallback tool parser ONLY when the stripped content is non-empty.
When gemma spends the turn reasoning and does not emit a structured call,
the stripped content is empty -> no call -> content_len=0/tool_calls=0
(exactly the real run), and the S2 nudge re-triggers the same derailment
(nudge-immune). This retires F18 (the "can't describe its own project"
reading was wrong — it CAN; it inconsistently fails to EMIT) and F18'
(envelope). The largest, most open-ended task in the flow is where the
model's reasoning-vs-action reliability floor shows.

**Disposition — no decomposition (sign-off pending).** Per the addendum,
world (b) forbids decomposition without chat-side sign-off, and the
diagnostic vindicates that hold: decomposition would have "fixed" a
mislabelled defect and buried F18''. The real fix is a DESIGN choice, none
made this run:
- (a) **emission reliability** — request gemma with tool-forcing / a
  greedy structured-call setting, or a two-step "reason then emit" turn so
  the drafted artifact in the thinking trace is actually written;
- (b) **recover the drafted artifact** — when a turn yields only a
  thinking trace that contains a fenced artifact for the expected output
  path, the adapter could surface it as a write (bounded, opt-in);
- (c) **the scheduled mechanical FACT-section extractor** — the
  file-map/verification-record/signature sections are mechanically
  derivable and should not be model-emitted at all; this both dodges F18''
  for the FACT sections and raises handoff quality (required for the
  brownfield phase-0 anyway).
Recommendation: (a)+(c). Run 2.4 pre-registration waits on the owner's
pick. P3-P7 stand as the regression suite; PROJECT_STATE.md still unwritten
(now known to be an emission-reliability gap, not an inability).

**Program status.** Every framework finding F1-F17b is closed or verified
live; F19 (config provenance) and F18'' (emission reliability) are the two
open threads, both framework-side. The model-capability question the
program kept circling is answered in gemma's favour: it wrote a correct
core, a working adapter, spec-faithful tests (via the tester seat), and —
shown here — a correct PROJECT_STATE draft; the remaining gap is getting
that draft reliably EMITTED through the tool channel at scale.

**Part 11 addendum — chat-side ruling (2026-07-05).**

**F18'' ratified; F18 and F18' retired.** The diagnostic establishes:
gemma drafts the complete, gate-passing document inside its reasoning
and inconsistently fails to emit the structured call (done_reason
stop, not length). Reasoning-vs-action emission gap at doc scale —
framework/reliability class, not capability. The adapter discards the
thinking that contains the finished work.

**F19 ruling — wire, don't delete.** Campaign params become explicit
overrides over cast-bound profile params (campaign = experiment
conditions; profile = model identity — the axis-1 orthogonality),
effective set logged at run start. Proof-by-use in 2.4: profile
max_tokens reverts to 2048; the campaign carries 4096; the run works
iff the wiring does.

**S7 registered — reasoning-salvage nudge.** Trigger: tool_calls=0 AND
stripped content empty AND thinking non-empty. Action: ONE re-prompt
carrying the model's own thinking draft verbatim + "emit the required
tool call now". salvage_budget default 0 (construct-nothing);
provenance: salvages_used, turns_reasoning_only. S2-family mechanism;
doctrine: the model's discarded draft is channel content.

**Run 2.4 pre-registration (bundling justified: F18''s cause is
established out-of-band by controlled diagnostic + seed reproduction;
the run's purpose is completion + handoff quality, not attribution).**
Deltas: (i) agora-handoff extractor — FACT sections (identity, file
map + AST signatures, verification record from gate commands,
capability inventory) generated mechanically; (ii) T9.2 becomes
PROSE-only: write PROJECT_STATE.prose.md (architecture & invariants,
conventions, extension points, how-to-run prose) — README-scale
single write; (iii) runner assembles PROJECT_STATE.md = FACT + PROSE
mechanically before the P9 gate (pure, deterministic, unit-tested);
gate checks the ASSEMBLED file, unchanged predicates; (iv) S7 armed at
salvage_budget 1 campaign-wide; (v) F19 wiring live. Worlds:
(a) P9 green -> RUN 2 COMPLETE -> PROJECT_STATE.md to the human
fact-check (FACT true-by-construction; the review measures PROSE).
(b) prose task derails AND salvage fails -> S7's first negative datum;
stop, chat-side. (c) extractor/assembly defect -> mechanical, loud,
fix-and-re-establish under the conditions-defect rule. One repair per
gate otherwise; waivers forbidden.

---

## Part 12 — run 2.4 findings (2026-07-06)

**Run 2.4:** the F18'' fix bundle landed and three of four deltas VERIFIED
live; the run stopped at P9 in **world (b)** — S7's first negative datum.
Every framework mechanism works; the residual is a model tool-EMISSION
floor that re-prompting does not force.

**F19 — VERIFIED live.** ``run_phased`` now resolves inference params as
profile <- campaign override and logs the effective set per model at each
phase start. The run's first lines: ``effective params [ollama/gemma4:e4b]:
... max_tokens=4096* ...`` — the campaign override reached the model while
the gemma-e4b profile identity stays 2048. The silently-inert knob (Part 11
F19) is wired and observable. Proof-by-use satisfied.

**Extractor + assembler — VERIFIED live.** The runner assembled
PROJECT_STATE.md = mechanical FACT + prose before the P9 gate. FACT is
correct-by-construction: Identity (echobot, runnable module), Capability
inventory (the REAL AST signature ``def handle_message(text: str, rng:
random.Random) -> str | None``), Verification record (the two gate
commands), File map (real tree + per-file top-level defs). All eight
assembled-file header predicates + both gate-command predicates PASS — the
assembler is sound and the FACT half of the handoff is now
true-by-construction. (World (c) — assembly defect — did not occur.)

**S7 — mechanism VERIFIED, outcome NEGATIVE (first negative datum).** T9.2:
salvages_used=1, turns_reasoning_only=3, tools_used=[]. The salvage fired
EXACTLY on its condition (reasoning-only turn, thinking 2683 chars) and
re-prompted with the draft verbatim + "emit the tool call now — no further
analysis." gemma produced three reasoning-only turns and never wrote the
prose file. **Re-prompting with the model's own draft does not recover
emission.** The construct-nothing / trigger-precision guarantees held
(unit-tested); the mechanism is correct, the hypothesis that a reminder
recovers the gap is falsified.

**F18''' — the emission gap is TERMINATION-BEFORE-ACTION, not forgetting.**
The decisive diagnostic (direct /api/chat, prose task, seed 42): gemma
returns done_reason=**stop**, 326 tokens, tool_calls=0, and a thinking
trace ending "...I will use a single write_file call ... Plan: 1.
Construct the markdown content string. 2. Use write_file to create
PROJECT_STATE.prose.md." — then TERMINATES without emitting the call. Not
truncation (stop, not length), not envelope (tiny), not inability (it
plans the exact call). gemma-e4b, on open-ended generative tasks,
non-deterministically ENDS THE TURN after reasoning to the intent, without
emitting the structured tool call. This is why S7 (a reminder) cannot fix
it — there is nothing to remind; the model has decided it is done. It also
explains the whole F18 family: code/test tasks (tight structure, short
reasoning) emit reliably; the open-ended handoff doc maximises reasoning
and so maximises the termination-before-action risk. The fix is not a
prompt — it is to FORCE the emission (structured-output / forced
tool_choice) or to remove the model from the FACT path (already done — the
extractor makes the FACT half model-free).

**Disposition — the handoff is now mostly solved; one lever left.** The
run's real yield: the FACT handoff is done and correct (mechanical,
verified), and the failure is isolated to one thing — getting gemma to
EMIT its prose through the tool channel. Options (run 2.5, owner's pick):
- (a) **tool-forcing** — request gemma with a forced tool call (Ollama
  ``tool_choice``-equivalent / structured ``format``) so the turn cannot
  terminate without emitting write_file. Highest-leverage; directly targets
  F18'''. S7 stays as provenance (turns_reasoning_only is the metric).
- (b) **FACT-complete handoff, prose optional** — the assembled
  PROJECT_STATE.md is already structurally complete and factually correct
  with placeholder prose; ship it as the handoff and make prose a
  best-effort/human section. The deferred "can the implementer describe its
  own project" question is then answered precisely: it can produce the
  FACTS (mechanically) but not reliably EMIT prose at this model size.
- (c) a stronger model for the single prose task (cast a doc-capable seat).
Recommend (a), with (b) as the ship-anyway fallback. P3-P7 stand as the
regression suite; PROJECT_STATE.md exists (FACT correct, prose pending).
Waivers forbidden; run did not complete.

**Program status.** Framework findings F1-F19 + S7 are closed or
verified-live; the sole open blocker is F18''' (tool-emission reliability),
now precisely characterised as termination-before-action and addressable by
tool-forcing rather than any prompt-level mechanism. gemma's capability is
not in question — it wrote the core, adapter, tests, and drafts the doc;
the gap is the structured emission of open-ended output.

**Part 12 addendum — chat-side ruling (2026-07-05).**

**F18''' ratified with SCOPING:** the floor is open-ended REFLECTIVE
doc emission (plans the call, terminates before action; reminder-
immune per S7's negative datum). It is NOT a doc-task floor: T9.1's
README (concrete ask, same phase/seat/model) passed first try. Roster/
casting record: gemma-e4b — concrete doc asks reliable; reflective
synthesis derails at this size.

**Tool-forcing declined for 2.5** (unmeasured daemon surface under
track-latest; F12 pattern class). Redirected to the Stage-3 battery:
"forced-emission reliability per model per daemon" joins the
edit-family sweep as a benchmark axis.

**S7 disposition:** kept, default 0. Mechanism verified (fires once,
draft verbatim); outcome negative on termination-decided turns; value
on continuation-intended reasoning-only turns unproven.

**Run 2.5 pre-registration — run 2 CLOSES at 2.5 either way (no 2.6).**
Deltas: T9.2 -> four micro-tasks T9.2a-d, one per prose section, each a
CONCRETE ask answerable from the project (3-8 lines, own output file
under prose/); assembler merges four files + FACT; P9 gate on the
assembled PROJECT_STATE.md unchanged; per-micro-task gate = file exists
+ non-trivial length. Worlds: (a) all four land -> P9 green -> RUN 2
COMPLETE -> human fact-check of the full artifact. (b) any micro-task
derails (one repair each, second red on the same task -> that section
falls back to binding: human, recorded, run CONTINUES to completion) ->
run 2 completes FACT-complete with mixed model/human prose; F18'''
stands as scoped. The fact-check happens in both worlds. Waivers
forbidden; budgets standard.

---

## Part 13 — run 2.5 findings — RUN 2 COMPLETE (2026-07-06)

**Run 2.5:** world (a). The four concrete prose micro-asks (T9.2a-d) each
landed FIRST TRY — no repair, no human fallback — the assembler produced
the full PROJECT_STATE.md, and **P9 went GREEN. Run 2 is the first full
P3->P9 completion in program history.** ``--status: next: done``.

**F18''' fix VALIDATED, and the finding sharpened by the provenance.**
Per-micro-task: salvages_used=0 and **turns_reasoning_only=0 for ALL
FOUR.** The reasoning-only derailment that blocked the eight-section
reflective task across runs 2.2-2.4 (turns_reasoning_only up to 3, S7
firing, done_reason=stop-before-emit) simply DID NOT OCCUR under concrete,
project-answerable asks. gemma emitted write_file directly every time; S7
never needed to fire. This is the decisive, clean confirmation of the
Part-12 scoping: **the tool-emission floor is open-ended REFLECTIVE
synthesis, not doc-writing.** Give the model a concrete question it can
answer from the project (as T9.1's README always was) and it stays in the
action channel; ask it to reflectively synthesize an eight-section
document and it plans-then-terminates. The fix was not a harness mechanism
(S7 kept, default 0, its value on continuation-intended turns still
unproven) but TASK DESIGN — the same lesson as F14's incremental build,
now applied to documentation. (Two micro-tasks took ~20 iterations of
corrective churn but never went reasoning-only — concreteness holds the
model in emission even through retries.)

**The deferred run-1 measurement — ANSWERED, affirmatively.** "Can the
implementer describe its own project accurately?" Yes: the completed
PROJECT_STATE.md carries four correct model-authored prose sections — the
architecture invariants (pure core, IO in the adapter, injected rng, frozen
signature), the conventions, the extension points, and the run/test
commands are all faithful to the code. The FACT half (identity, capability
inventory via AST, verification record, file map) is true-by-construction.
The artifact is fact-checkable and correct; the human fact-check is now a
review of a real, complete handoff rather than a measurement of whether one
can be produced at all.

**Doctrine yield — the handoff pattern.** A machine-consumable handoff
document for a small-context model should be built as: (i) FACT sections
generated mechanically from the tree + gates (never model-emitted —
hallucination-proof and reliability-proof), assembled deterministically;
(ii) PROSE sections decomposed into per-section CONCRETE asks, each
answerable from the project, each its own tiny gated task, with a
human-fallback that keeps the document structurally complete. This is the
required quality bar for the brownfield probe's phase-0 re-validation and
generalises beyond echobot.

**RUN 2 CLOSED.** Every finding F1-F19 + S7 + the F18 family is closed or
verified-live; there is no open framework blocker. Net arc of run 2
(greenfield): P4 whole-file-rewrite regression (F12xF14, repaired), P5
spec-underspecification (F15, spec-doctrine fix), P6/P7 adapter
import-contract + weak-gate + error-swallow (F16/F17/F17b, fixed), P9
tool-emission floor (F18 -> F18' falsified -> F18'' -> F18''' scoped ->
fixed by concrete micro-asks). The instrument found a real, previously
unreachable class at every phase and closed it; the bot exists, passes its
own tests end-to-end, runs headlessly, and describes itself. **Next: the
pre-registered brownfield probe** (extension spec — new commands + a real
Discord adapter against a fake gateway; phase-0 opens by re-validating this
PROJECT_STATE.md's gates, with a deliberate stale-file red-team). That is a
new program, off this completed baseline.

**Part 13 addendum — chat-side fact-check + RUN 2 CLOSURE (2026-07-05).**

Fact-check performed by re-execution and code inspection. VERDICT: PASS
with two corrections. FACT: capability inventory + file map verified
against tree/AST; verification record cmd 1 re-ran green (8/8); cmd 2 is
F20 — the extractor serialized the P7 run_check as bare argv, dropping
stdin + expectation; NOT faithfully re-runnable as recorded (phase-0
blocker class). PROSE: architecture/invariants verified claim-by-claim
in code; extension points + how-to-run correct; Conventions contains
ONE false claim ("plain lowercase" vs actual sentence-case strings,
core.py L38/L47) — F21: the claim was invented by the T9.2b micro-ask,
not by the model; rule adopted: micro-asks must cite spec/code anchors.
F20b: assembler write encoding unpinned; verify + pin utf-8.
Fact-check meta, recorded for honesty: the checker's own probes
misfired twice (CRLF pipe; relative-path .NET read) and were corrected
against authoritative methods — instrument discipline applies to the
fact-checker too.

Corrections directive (artifact stays fully model-authored): fix F20
(extractor serializes complete run_checks: cmd + stdin + expectation),
fix the T9.2b ask (anchor its convention claims to code/spec), pin
utf-8 in assembler; re-run T9.2b + re-extract + re-assemble ->
PROJECT_STATE.md v1.1; spot-verify the corrected lines. Adapter nit
for the brownfield backlog (not a run-2 defect): __main__ rstrip('\n')
keeps '\r' on CRLF input — portability hardening later.

**RUN 2 CLOSED — COMPLETE.** P3-P9 green; bot built, tested,
acceptance-passed, documented, handed off. The greenfield program ends
at F1-F21: nineteen framework/planner defects fixed structurally, two
scoped model boundaries measured (F14 completeness on whole-file
synthesis; F18''' reflective-emission, dodged by task design), zero
unresolved blockers. Baseline for the brownfield probe: tag the
corrected-artifact commit (suggest: echobot-v1).

---

## Part 14 — run 3 (brownfield probe) findings — RUN 3 COMPLETE (2026-07-06)

**Run 3:** the brownfield probe SUCCEEDED (world (a)). The implementer returned
to the completed echobot-v1, re-validated the brief, navigated via it, extended
the project (two commands + a transport-injected Discord adapter), broke nothing,
and handed off PROJECT_STATE.md v2. P0-P9 green; two conditions defects found and
fixed under the standing rule; one runner backlog item; no model-capability floor.

**Headline measurement — brief-as-index navigation: AFFIRMATIVE (3/3 edit tasks).**
Every implementer edit task read PROJECT_STATE.md BEFORE touching code (run.log):
P4 (read brief -> read core.py -> extend), P6 (read brief -> write adapter), P9
(read brief -> write prose). The returning model used the handoff document as its
navigation index, did not read the old spec, and never touched the frozen core
signature. The casting/handoff architecture's reason to exist — a small-context
model returning to a project and navigating by the brief — is demonstrated.

**F20 verification record is a LIVE protective gate (P0 red-team).** Before task
one: staling one source byte (removing "NdM" from a usage string) red the P0
re-validation (pytest caught it via test_roll_malformed); restoring it greened P0.
A protective claim is only trusted after it is seen failing — it was. The runner's
--phase0 parses the brief's F20 fenced run_checks and executes them; the round-trip
built in run 2's corrections is now load-bearing.

**F22 — navigation/map-pointer discipline must cover EVERY editing seat.** The
flow carried the map-pointer + frozen-signature contract to the IMPL tasks (which
navigated correctly) but NOT the TESTER tasks. Ungifted the pointer and the real
API, the tester never read the brief/core and FABRICATED a non-existent
`echobot.core.execute_command(text, random=...)` (real API: handle_message(text,
rng)) — run-1's F6/F8 spec-channel starvation, recurring tester-side. Model
exonerated; verified conditions defect. Fix: T5.1/T6.2 gain the map-pointer +
inline signature; re-established green (no budget). Doctrine: the brief is the
navigation contract for ALL seats, not just the implementer.

**P6 send-channel under-specification (F15/F6 class, conditions defect).** The
delta spec's adapter contract said events have `.content` and `send(channel, text)`
but never said where the channel comes from. The implementer guessed the event
twice (`event.channel` -> crash; `event.channel_id` + a guard -> silently dropped
the send). Fix: T6.1 states the channel is best-effort (getattr(event,"channel",
None)) and the non-None response must ALWAYS be sent; re-established green
(adapter 14/14). An acceptance predicate that names a parameter must say where the
parameter comes from.

**F23 — runner backlog: same-phase repair of a non-blocker task records a FALSE
phase green.** When a phase's gate blocker (T6.2 pytest) is a DIFFERENT task from
the one carrying the defect (T6.1 adapter), `--rerun-task <defect-task>` evaluates
only the reran task's own postconditions (which pass) and records the phase green
without re-running the blocker. Cross-phase repair already re-evaluates the whole
gate mechanically (reevaluate_phase_gate); same-phase repair should too. Worked
around here by re-running the blocker task after the fix; noted for a runner fix.

**Regression discipline held.** The 8 baseline core tests stayed green through
every phase (the regression suite, free of charge per the phase plan); the frozen
core signature was never altered; the adapter did not modify core. Convention
adherence held: new command strings are sentence-case per the brief's (run-2.5-
corrected) Conventions; new tests follow test_<behaviour> naming.

**PROJECT_STATE.md v2 (FACT re-extracted, prose model-authored).** The mechanical
FACT sections re-extracted cleanly over the new tree (capability inventory + file
map now carry discord_adapter.py's Gateway/Event/run_adapter and the four new
tests + the adapter contract tests); only the CHANGED prose section
(extension_points) was re-authored (cites the concrete discord_adapter +
run_adapter); architecture/conventions/how-to-run prose reused from v1.1. The
handoff-doctrine (mechanical FACT + concrete per-section prose micro-asks) carried
from greenfield to brownfield unchanged. v2 awaits the chat-side fact-check;
tag echobot-v2 after it passes.

**Executor note (honesty).** One stray `--rerun-task` ran against the run-2
campaign mid-P6; the closed run-2 workspace was reset --hard to its echobot-v1
state (8/8), a stray P6 ledger line left as provenance. Run 3's baseline was copied
before this, so run 3 is unaffected. Instrument discipline applies to the executor.

**Program status.** Greenfield (runs 1-2) built and handed off echobot-v1;
brownfield (run 3) returned, extended, and handed off v2 — the full lifecycle the
casting/handoff architecture was designed for. Findings F1-F23. The measured model
boundaries (F14 whole-file synthesis; F18''' reflective doc emission) are dodged by
task design; every framework/planner defect is fixed structurally. Milestone: the
handoff brief works as a navigation index for a returning small-context model.

**Part 15 — run 3 fact-check + corrections directive (2026-07-05).**

Fact-check of PROJECT_STATE.md v2 by re-execution + inspection.
Behaviour: 14/14 pytest; flip deterministic/valid; choose valid; help
lists both new commands. VERDICT: PASS with corrections.

**F24 — reuse is not revalidation.** Two reused v1.1 prose claims went
stale exactly where the delta touched them: (V2-1) "IO confined
exclusively to __main__" — false since discord_adapter.py; (V2-3)
"tests must be placed in tests/test_core.py" — false since
test_discord_adapter.py. Rule adopted: at re-handoff, every REUSED
prose section passes a staleness screen against the delta — a section
is re-asked iff the delta touches its subject nouns (both stale
sections name "adapter"/"tests"; the screen is near-mechanical).

**V2-2 — verification-record completeness.** The record carries only
v1's four checks; the P7' FakeGateway round-trip and P4' flip/choose
smokes are absent — a future phase-0 verifies the extension only
indirectly via pytest. The extractor must derive the record from the
PRODUCING flow's full run_check gate set, with a regression test
asserting record-coverage == flow run_check gates.

Corrections (artifact stays model-authored): C1 re-author architecture
micro-ask, anchored ("IO lives in adapter modules — __main__ and
gateway implementations; core has none"); C2 conventions sentence
(core tests in tests/test_core.py; adapter contract tests in
tests/test_<adapter>.py); C3 re-extract verification record from the
run-3 flow (expect ~7-8 checks incl. round-trip + smokes) + the
coverage regression test; byte-confirm utf-8; re-assemble v2.1; THEN
tag echobot-v2. Backlog (before any next run): F23 same-phase-repair
false-green fix; closed-run workspace guard (runner refuses
--rerun-task on a campaign whose ledger is complete); code nit:
Optional[any] annotation in discord_adapter (typing, non-behavioural).

Milestone on record: the full lifecycle — greenfield build -> handoff
-> phase-0 re-validation (red-team proven) -> brownfield extension via
brief-as-index navigation (3/3) -> re-handoff — is demonstrated. The
arc document and push milestone unblock upon v2.1 + tag.

---

**Part 16 — run 3 v2.1 corrections applied → echobot-v2 (2026-07-06).**

Executed the Part-15 directive on branch feat/integration-run-3. Suite 1474
passed / 8 skipped, ruff clean. PROJECT_STATE.md v2.1 assembled, 13/13
verification-record checks re-run green (out of band). Tag echobot-v2 on the
corrections commit.

**C1/C2 (re-anchored prose, model-authored).** turns_reasoning_only=0 on every
authoring turn — the emission floor did not recur (concrete asks hold, as run 2.5
established). C2 (conventions) passed first-pass. C1 (architecture) surfaced F25.

**F25 — a seeded/pre-existing target file defeats an overwrite via the write-once
guard, and the disk size-gate can be fooled while the artifact predicate catches
it.** The corrections workspace was a COPY of the completed tree, so the target
prose files already existed. Tc1 EMITTED write_file with correct anchored content,
but the harness overwrite-guard rejected it (`already exists ... write_file
disabled`) and the turn auto-mark-completed with no artifact. The `os.path.getsize
>= 120` run_check PASSED anyway (the seeded stale file satisfied it) — a false-green
had that been the only gate; the artifact-tracking `file_exists` predicate correctly
FAILED (no artifact produced). Lessons: (a) when re-authoring an existing file, either
clear the target first or ensure the seat can force-overwrite; (b) a size/existence
run_check over a pre-seeded workspace is not a proof of authorship — pair it with the
artifact predicate. Repair: removed the stale target, `--rerun-task Tc1 --oracle P9c`
→ GREEN.

**F23 fix — demonstrated live.** The Tc1 repair (same-phase, `--oracle P9c`) re-ran only
Tc1 by the model, yet the gate re-checked BOTH Tc1 and Tc2 mechanically over the workspace
(Tc2 PASS from its still-on-disk artifact). Previously same-phase repair evaluated only the
re-run task and could green a phase whose different blocker still failed. Now
`repair_gate_is_mechanical(rerun_task, oracle_phase)` routes ALL repairs through the
full-gate re-eval. Unit test: a passing non-blocker cannot green a phase whose blocker still
fails.

**C3 — verification-record completeness + a latent serializer bug.** `flow_gate_checks`
now returns the producing flow's FULL run_check gate set (run-3 brownfield: 13, was 4),
excluding only `pytest --collect-only` (meta) and handoff-scaffolding checks (`prose/`,
`PROJECT_STATE`). Regression test: record-coverage == flow gate set, and specifically the
FakeGateway round-trip + `!flip`/`!choose` smokes are present (the v2-record fixture, v1's
4 checks, is a strict incomplete subset → fails; the v2.1 set passes). Deriving the record
from the full set exposed a real bug: a run_check whose command has embedded newlines (the
round-trip `python -c`) produced a multi-line `# `-comment, whose non-`#` continuation lines
leaked into the JSON body and made the parser DROP the spec. Fixed `_human_command_line`
to collapse `\n`→`\n` (one physical comment line). This check was absent from the old
4-check record, so the bug was latent until completeness forced it in — completeness is
itself a bug-finder.

**Closed-ledger guard.** `run_phased.py` refuses `--rerun-task` when the campaign ledger
reads complete (all gates green/waived) — the direct fix for the run-2 incident (a stray
rerun rewrote the shipped echobot-v1 tree). Corrections run on a FRESH campaign/output_dir.
Test included (complete, incomplete, and waived-red ledgers).

**C3 policy note (scope decision).** The Part-15 "~7-8 checks" estimate under-counted the
flow's inline behavioural asserts; the implemented policy is the FULL run_check set minus
collect-only + scaffolding (13), pinned by the coverage test rather than a hand-tuned list —
so a future flow that adds a behavioural run_check the record misses will fail the test.

Lifecycle now fully closed: greenfield build → handoff (v1.1, echobot-v1) → phase-0
re-validation (red-team proven) → brownfield extension via brief-as-index navigation →
re-handoff (v2 → fact-check → v2.1 corrections, echobot-v2). No open framework blocker.

## Part 17 — lifecycle baseline: first clean end-to-end run (2026-07-15)

*Session log: `docs/runs/lifecycle-baseline/session-log.md` (verbatim gates,
per-task provenance, live bot transcript). Executed at `5ab8950` on
`chore/integration-hardening` (`echobot-v2` is an ancestor — the full fix stack
is present). Provenance rule applied to this entry: every configuration claim
below is quoted from the run's **effective-params log**, never from the campaign
file — `campaigns/integration-run-2.yaml` today reads `max_tokens: 4096`, but run
2.0 **executed** at 2048 (the 4096 was written back after the run-2.3 envelope
experiment), so the file cannot testify to what any past run actually did.*

**The first single-session, zero-repair P3→P9 traversal in program history.**
All six gates green in 32m 25s, no repair budget consumed, no waiver, no spec
amendment, no operator intervention beyond one `--next` per phase.

```
[*] effective params [ollama/gemma4:e4b]:          num_ctx=8192* max_tokens=4096* temperature=0.0* seed=42*
[*] effective params [ollama/qwen2.5:7b-instruct]: num_ctx=8192* max_tokens=4096* temperature=0.0* seed=42*
harness {corrective, nudge 1, review 0, salvage 1} | cast p40-24gb | Ollama 0.31.1 | Python 3.14.3
=== integration-run-2 — phase status ===
  P3 green | P4 green | P5 green | P6 green | P7 green | P9 green (mechanical re-eval)
next: done (all phases green or waived)
```

Every prior "completion" was a **program, not a run**: run 2 reached `next: done`
only at run 2.5 — six executions across two days, ~5 repairs, an F15 spec
amendment, and code/flow fixes committed *between* runs. This is the first time
the accumulated stack ran **as a stack**, on a fresh ledger, in one sitting.

**Fix-stack-as-a-stack — every fix verified live, simultaneously.** The value of
this run is not that it went green; it is that the fixes stopped needing each
other's absence to work:

- **F13** invariant: the allowlist filtered 13 tools on all **10 implementer
  tasks**; **none** logged `hid write_file`. The hide fired only on unrestricted
  seats — the tester (T5.1) and every verifier. Both behaviours correct, matching
  the Part-8 record exactly.
- **F6**: `tests/test_core.py` imports the real `handle_message`, **zero** mocks.
  World (a) on the first pass.
- **F15**: the malformed-roll reply carries the literal `NdM` substring; the
  tester asserted it. Green with no amendment — the defect that *stopped run 2.0*
  did not recur.
- **F16/F17**: `__main__.py` line 3 imports `handle_message`; **no bare `except`**
  anywhere. Both run-2.1 blockers absent first pass.
- **F18''' / task design**: T9.2a–d each first try, `turns_reasoning_only=0`.
- **F19**: `max_tokens=4096*` confirmed reaching both models.
- **F17b**: 6 mechanical re-eval records persisted (23 records = 17 executed + 6).
- **F10**: the `!ping` / `!roll 2d6` behavioural asserts are what proved P4.

**Emission floor: silent this run — S7 armed and never needed.** Across **all 23
records**: `turns_reasoning_only=0`, `salvages_used=0`, `nudges_used=0`,
`tool_calls_malformed=0`. `salvage_budget: 1` was active and never fired. (Four
turns show `tool_calls=0 content_len=0`; each is its task's **final** turn — the
normal loop-termination signal after `mark_complete`, not F18''' derailment. The
distinction is exactly why the provenance field exists and why raw log greps must
not be read as reasoning-only turns.)

**Verifier series — updated, and the envelope hypothesis REJECTED for it.**
This run: **V3.1 pass (valid `verdicts/p3.json` — first time in program
history)**, V4.1 pass, **V5.1 fail (malformed)**, **V6.1 fail (malformed)**,
V7.1 pass, V9.1 pass. V3 flipped at 4096; **V5 and V6 did not**. So the
long-standing "V3/V5/V6 malformed, V4/V7/V9 valid — instruct phase-inconsistent"
backlog item **stands**: the envelope does **not** explain the verifier-fidelity
gap. Only its V3 clause is retired. Verifier failures gate nothing (non-blocking
by design) and were recorded, not waived.

**F14-at-P4 — candidate re-read, NOT rewritten.** T4.2 produced correct `NdM`
regex parsing **first pass** at 4096. Both prior 2048 attempts failed it: run 2.0
dropped `!roll` entirely (Part 8), and a 2026-07-15 attempt on the discarded
run-1 campaign wrote `!roll` with space-separated `N M` grammar and then no-oped
its repair. That is suggestive, not established — **this run changed more than one
variable** (envelope 2048→4096, salvage 0→1, Python →3.14, both-model
co-residency). The F14 P4 clause is **left standing** pending the pre-registered
A/B (2048 vs 4096, T4.2 only, n=3 each; `docs/design/deployment-reconciliation.md`
Phase 1). Note F18' already *falsified* the envelope hypothesis at T9.2 — so any
envelope effect is task-dependent, and a P4-scoped result must not be generalised.

**Not a determinism claim.** At identical seed/params, the earlier same-day
attempt drew a *different* T4.2 defect than run 2.0's. One sample, not a fixed
point.

**Confounds and deviations** (full list in the session log): Python **3.14.3**
(no 3.12 on the box; above the `>=3.12` floor, never previously exercised —
passed); prewarm loaded both models at **32768**, the first task call reloaded
gemma at the pinned **8192** (redundant load, not a fidelity loss);
`OLLAMA_MAX_LOADED_MODELS=2` against `OLLAMA.md`'s `=1` (the cast's 14.6 GB
co-residency fits 24 GB; `=1` thrashes on every verifier task); Ollama on `:11700`
because port 11434 sits inside a WinNAT reserved range (11420–11519) on this box —
absorbed by a one-line `AGORA_OLLAMA_BASE_URL` change, an unplanned live proof of
the single-source config design; T7.1 logged 4 `tool_call_unknown_name` events and
still passed (roster quirk, backlog); `duration_s` is unpopulated on every record
(wall-clock derived from `run.log`).

**T9.2d reads `failed` in its executed record and passes on mechanical re-eval** —
its postconditions include the assembled `PROJECT_STATE.md` headers, evaluated
before the runner assembles `FACT + prose`. Designed assembly order, not a masked
failure; the `(mechanical re-eval)` tag on the P9 gate records it.

`PROJECT_STATE.md` is **fully model-authored** — zero `(human)` placeholders — and
its architecture prose independently restates the frozen-signature/pure-core/
injected-rng invariants. The artifact runs: `printf '!ping\n!roll 2d6\n!help\n' |
python -m echobot` → `pong` / `rolled 2d6: 5+5=10` / the full help listing.

**What this establishes for the deployment work.** The framework holds end-to-end
on the validated path. Every failure chased on 2026-07-15 before this run was
configuration, environment, or documentation — never the framework: a discarded
campaign (`integration-run-1.yaml`, no `salvage_budget`, 2048) that disabled the
very mitigation its repair then needed; a dead Conduit against an unguarded
`build_matrix_client` await; and a demo path the program never drove. Baseline
tagged `lifecycle-baseline-1`.

**F26 — a config file is a moving target; only the effective log testifies.**
Campaign/profile/flow YAML is edited across a program's life, so the file as it
stands today does **not** record what any past run executed. `integration-run-2.yaml`
reads `max_tokens: 4096` now, but run 2.0 *executed at 2048* — the 4096 was
written back after the run-2.3 envelope experiment. Reading the current file as
run 2.0's conditions inverts the F14-at-P4 evidence entirely. This is not
hypothetical: on 2026-07-15 an executor picked `integration-run-1.yaml` believing
it equivalent, thereby silently disabling `salvage_budget` and halving the
envelope — and the resulting repair failed in exactly the mode the disabled
mitigation exists to catch. **Doctrine:** every findings entry, report, or
comparison cites the run's **effective-params provenance** (`[*] effective params
[...]` in the runner output, mirrored into `run.jsonl`), never the file path.
Where a claim depends on conditions, quote the log. F19's param wiring is what
makes this auditable; F26 is the obligation to actually use it. Corollary for
**executors**: identify a campaign by what it *ran* (provenance), not by a
filename that looks equivalent.
