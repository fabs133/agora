# Measuring Without Fooling Yourself

## From tool-call probes to a self-documenting software lifecycle on local models

*Draft v0.91 — 2026-07-06 (post claims-audit corrections). Fabio-Eric Rempel (ORCID 0009-0008-8447-6159).
Companion record: this repository. All claims cite committed artifacts;
readers who trust nothing can re-run the verification records and read
the pre-registrations in commit order.*

---

## Abstract

Small, locally-hosted language models are widely believed to be
unreliable tool users. This report documents a measurement program that
began with that belief, spent seven probe versions discovering that
most of the unreliability lived in the measurement apparatus, the
serving stack, and the task channel — and ended with a 9.6 GB model
carrying a complete software lifecycle: greenfield build, machine-
consumable handoff, brownfield extension navigated through its own
handoff document, and re-handoff. Of twenty-five numbered findings in
the integration program (plus the axis-1 and reliability findings that
preceded them), all but two were defects of the framework, the
specification, or the process. The two that remained were genuine,
narrow model boundaries — and both were dismantled by task design
rather than by larger models. The contribution is not the bot the
program built; it is the method that made every failure attributable:
pre-registration before execution, phase gates with re-runnable
records, provenance as the only source of truth, and a standing rule
set that decides — before results exist — what may be retried, what
counts as falsified, and what may never be waived.

## 1. The instrument stack

Every result in this report rests on the same small set of practices,
adopted early and never suspended.

**Pre-registration.** Hypotheses, per-model expectations, falsification
criteria, and decision gates were written and committed before
execution (`docs/research/prompting-strategies.md`,
`docs/research/harness-reliability.md`, the run pre-registrations in
`docs/runs/` and `docs/integration/`). Where a criterion later proved
insensitive, the verdict was still decided by the original criterion
and the insensitivity recorded separately (the mistral-nemo case,
axis-1 v2 findings §4.2). Where a mid-program amendment was needed, it
was made openly, with its precedent named (the "conditions-defect
re-establishment" rule, three uses before it was named, findings Part 8).

**Provenance as truth.** Every run emits JSONL records (schema-
versioned, additive-only changes); every derived number is regenerable
from them; every campaign records daemon version, git commit, and
effective parameters. When the phased integration runner initially
shipped without the observer attached, the resulting invisibility of seventeen identical tool rejections (F1's evidence) became finding F3 — and the rule
that *errors must leave evidence, in every runner, always*.

**Gates that can be re-run.** Task postconditions are mechanical
(file checks and `run_check`: a command, its stdin, its expected
output). A completed project's handoff document embeds its gate
checks verbatim in re-runnable form; the brownfield program's phase-0
re-executes them — and was trusted only after a deliberately staled
file was observed to turn it red (run 3, red-team protocol).

**Standing rules.** One repair per gate; a second red on the same gate
stops the run. Waivers are forbidden in unattended execution. A trial
whose conditions contained a verified defect re-establishes without
consuming budget; a trial that reached the model under valid conditions
consumes it. These rules were cheap to write and expensive to honor —
and honoring them is why the record below can be believed.

## 2. Axis 1: measuring tool-call fidelity without lying to yourself

The program began with a conventional question — which of six local
models (4.7–18 GB per the local manifest store, one Ollama daemon, temperature 0, fixed seed) can
emit reliable tool calls — and a conventional probe: three file-
manipulation tasks with mechanical postconditions, 36 runs (v1).

The v1 results looked like model findings: a coder family that
narrated instead of calling, a Mistral variant that split emission
between channels, one strong performer. The next six probe versions
progressively retracted the frame:

- **v2** introduced same-daemon A/B strategy arms and drift sentinels —
  and immediately paid for itself: a daemon upgrade (0.24 → 0.31.1) had
  silently broken v1's "model sizes are byte-identical" finding, while
  the sentinel model was byte-identical across daemons. Comparisons
  across serving-stack versions are comparisons of the stack.
- **The stale-output forensics** then retracted nearly every recorded
  success in the program's history: the probe workspace was never reset
  between runs, and a write-once guard both blocked live writes and
  disabled the write tool for the rest of the task. Transcript-level
  classification found **2 live passes in v1 against 46 stale-backed;
  0 live in v2 against 80; 0 in v3.0 against 30** (axis-1 v2 findings
  §8). The best model's signature "failure" was a guard artifact over
  byte-correct attempted content.
- **The determinism probe** isolated content non-determinism at
  temperature 0 to a single character position — the join between two
  observed files — surviving serialized execution and forced cold
  loads. The mechanism: near-tie greedy decoding decided by GPU-level
  floating-point jitter, where near-ties exist only where the harness
  transmits information weakly (`docs/runs/determinism-probe/`).
  Trajectory determinism held 15/15 throughout; content determinism
  returned the moment the rendering made the deciding byte salient.
- **v5–v7** made the observation channel byte-transparent: the
  tool-result marker that models faithfully copied into artifacts, the
  Windows text-mode newline translation, and a daemon rendering branch
  that re-encoded content — each removed, each behind a one-variable
  rerun. The models copied whatever they were shown, exactly; the
  channel had been lying in six distinct, now-enumerated ways (`docs/runs/rendering-series-findings.md`).

On the seventh probe version, one model recorded the program's first
genuine, live, byte-honest 9/9 — and the claim finally meant something,
because every way the apparatus could have manufactured it had been
found and removed.

## 3. Harness levers, sorted by the failure modes they actually fix

Four bounded mechanisms were pre-registered and tested; their fates
differ, and the differences are the doctrine.

| Mechanism | Target | Verdict |
|---|---|---|
| S1 corrective tool errors | malformed calls | works (malformed → 0 where it applied) |
| S2 completion nudge | empty-turn stalls | works, narrowly — and only for stalls |
| S6 completion review | wrong-byte completion | falsified: reflection without an oracle cannot fix an error the model does not perceive |
| S7 reasoning-salvage | reasoning-only turns | scoped negative: a turn the model considers finished cannot be reopened by reminder |

The S2 verdict carries an erratum worth reading in full (v3.2 findings):
the mechanism was first declared useless, and an accidental two-variable
campaign later revealed it had been quietly earning one task cell's passes outright (coder-14b's small_chain, 3/3, nudge-dependent). The general lesson survived both verdicts: **the
mechanisms that worked improved what the model observes; the mechanisms
that failed told the model what to do.** One instruction-shaped
mechanism survives, for exactly one failure shape — honest doctrines
accrue footnotes.

## 4. Integration: the same gate, five causes

The first integration run — a small command-router bot, built phase by
phase behind mechanical gates — stopped at its testing phase five times.
Each stop had a different cause, each cause was structural, and none
was the model:

1. A legacy role-permission rule silently rejected every write to
   `tests/` (seventeen identical rejections, invisible until the
   observer was attached).
2. The test-authoring task referenced a specification it was never
   shown; the model fabricated an API and tested its own mocks.
3. The implementer's task never contained the function contract — so
   its "drift" was invention in the absence of one, and an oracle-fed
   repair of a context-starved task re-ran the starvation.
4. The model's correct repair died in the arguments of a tool nobody
   had ever measured it on — while the tool it *was* measured on had
   been hidden from its manifest by a guard.
5. The fix for (4) collided with that guard to produce a seat with zero
   write affordances.

Run 1.5, with every channel finally provisioned — contract, authority
clause, local gate, verbatim oracle, offered affordance — produced the
program's first end-to-end repair: the model refactored a class-shaped
core into the specified pure function and landed it. The exoneration
ledger closed at five framework causes, zero model floors, and one rule
that generalizes: **check the channel before charging the model.** In
this program, the channel was guilty first in roughly nine cases out of
ten.

## 5. The two real boundaries, and how task design dismantled them

**F14 — whole-file synthesis completeness.** Rewriting a complete file
from contract, the 9.6 GB implementer got 4 of 8 behaviours right in
one shot. Built incrementally with per-task smoke gates, the same model
reached 6/8 on first pass and 7/8 after a single named-oracle repair
(run 2.0). The boundary is real; the operation is optional.

**F18‴ — reflective-document emission.** Asked for an eight-section
project handoff in one task, the model drafted the complete, gate-
passing document *inside its reasoning* and terminated without emitting
the call (`done_reason: stop`, verified by direct diagnostic; a
re-prompt carrying its own draft did not reopen the turn). Asked four
concrete questions — each answerable from the project, each a small
write — it emitted directly, zero reasoning-only turns, four for four
(run 2.5). The floor is open-ended reflective synthesis, not document
writing.

Neither boundary was dismantled by a bigger model, a new prompt trick,
or a harness mechanism. Both fell to the same move: **fit the operation
to the measured surface.** That move is only available to a program
that measured the surface first.

## 6. A handoff that survives re-execution

The lifecycle's hinge is `PROJECT_STATE.md`: FACT sections generated
mechanically (file map with AST signatures, capability inventory,
verification record as re-runnable checks), PROSE sections written by
the model through concrete asks, assembled deterministically, and
fact-checked by re-execution. The brownfield run (run 3) opened by
re-running the record's checks — after first proving they *can* fail,
via a deliberately staled source file — then extended the project with
the handoff as its only map. The headline measurement was affirmative
in three of three editing tasks: the model read the handoff first,
navigated by the file map, never opened the original specification,
and never touched the frozen core signature. The old test suite rode
along as a free regression gate; the extension shipped with fourteen
tests green and a re-extracted handoff.

Two lifecycle findings matter beyond this project. **F24 — reuse is not
revalidation:** two prose claims reused from the previous handoff went
stale exactly where the extension touched their subjects; re-handoff
now screens every reused section against the delta. And the
verification record must be derived from the *producing* run's full
gate set — a coverage gap here means a future phase-0 silently protects
less than the project does (caught by fact-check, closed by a
regression test that fails on the defective fixture).

## 7. The audit symmetry

The findings' addresses moved in a straight line, and the line is the
argument. The instrument first cleared its own noise (framework
findings, probe versions 1–7, integration causes 1–5). Then it measured
the machine (F14, F18‴ — the only two model-side findings, both
scoped, both dismantled). Then it turned around: it found an ambiguity
in the human-authored specification (F15 — a requirement with no
acceptance predicate, red-ing a gate on a semantically correct answer),
an invented convention in a human-drafted micro-ask (F21), and, twice,
defects in the fact-checker's own probes (a CRLF pipe and a
relative-path read, both recorded in the closure notes). A measurement
program that eventually finds bugs in its author's spec and its
auditor's instruments — and logs them with the same prominence as the
model's — is the operational definition of one that is not fooling
itself.

## 8. Limitations, and the program this one designed

The implementer evidence covers one model family on one probe's tool
surface; the verifier seat remains unreliable at its artifact protocol
(a four-point fidelity series, deliberately left unfixed as
measurement); the lifecycle was demonstrated on one small project
shape. These are not caveats appended for form — they are the next
program's specification. Nearly every axis of the planned benchmark
battery was designed by a specific failure recorded here: the
edit-tool-family sweep (F12: casting onto unmeasured surface), forced-
emission reliability (the F18 family), concrete-vs-reflective document
tasks, output-envelope scaling (F18′/F19), and per-(model × tool-
surface) capability keys. The intended end state is mundane and, for
that reason, valuable: pull a model, run the battery, and cast it into
seats it has evidence for — with the casting file citing the rows.

---

## Appendix A — Integration findings index (F1–F25)

One line each; authoritative text in `docs/runs/integration-run-1/
findings.md` Parts 1–16. Axis-1, forensics, determinism, and
harness-lever findings live in their own documents under `docs/runs/`.

F1 two role systems collided (legacy turf rule vs casting taxonomy) ·
F2 task↔permission feasibility is statically checkable (now a load-time
lint) · F3 every runner attaches the observer · F4 signature drift (its cause — a contract never delivered — is F8) · F5 verifiers must run regardless of task
outcome (`order_after`) · F6 specification-channel starvation · F7
repair quality is bounded by oracle expressiveness · F8 the implementer
never had the contract · F9 repair inherits the original task's context
· F10 local gates must red on local contract violations · F11 head+tail
oracle capture · F12 tool surface is part of the casting evidence key ·
F13 the overwrite guard may never reduce a seat to zero write
affordances · F14 whole-file synthesis completeness (model boundary,
scoped) · F15 spec self-testability: every requirement carries its
acceptance predicate · F16 the adapter repeated the missing-contract ×
weak-gate pattern · F17 defensive error-swallowing starves the repair
oracle (F17b: mechanical re-evals must persist their captures) · F18
family → F18‴ reflective-emission boundary (model, scoped; dismantled
by concrete micro-asks) · F19 config-provenance trap: an inert
parameter surface is worse than a missing one · F20 verification
records must be re-runnable verbatim (F20b: pin encodings) · F21
micro-asks must cite spec or code anchors · F22 the map-pointer
discipline covers every editing seat · F23 same-phase repairs re-
evaluate the full gate · F24 reuse is not revalidation · F25 a seeded
pre-existing target defeats overwrite-guarded repair.

## Appendix B — Runs ledger (summary)

Axis-1: v1 (36 runs) · v2 A/B + sentinels (40) · v3.0 corrective (15) ·
v3.0.1 (3; probe v4 ran under it) · v5/v6/v7 rendering series (gemma blocks, 5 each) · determinism
A/B/B2 (15) · v3.2 nudge (15) · v8 review (15). Integration: runs
1–1.5 (P5 arc, repair landed at 1.5) · 2.0–2.5 (greenfield complete;
baseline tag `echobot-v1`) · run 3 (brownfield complete; tag
`echobot-v2`). All provenance under `runs_out/`; session logs and
findings under `docs/runs/`.

## Appendix C — Standing rules (verbatim)

One repair per gate; second red on the same gate stops the run. Waivers
forbidden in unattended execution. Conditions-defect re-establishment:
a trial invalidated by a verified conditions defect re-runs without
consuming budget. Pre-registrations are committed before execution;
verdicts are decided by the registered criteria; amendments are made
openly with precedent named. Errors leave evidence in every runner.

## Appendix D — Reproduction

Everything in this report regenerates from the repository: raw JSONL
under `runs_out/`, analysis via `scripts/analyze_layer2.py` and the
forensic/autopsy tooling, handoff verification via the F20 records in
`PROJECT_STATE.md` (tags `echobot-v1`, `echobot-v2`). The suite
(`pytest`, ~1470 tests at time of writing) is green at every commit
cited.
