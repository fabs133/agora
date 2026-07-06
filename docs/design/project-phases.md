# Phases of project work — taxonomy and framework gap analysis

*Drafted 2026-07-03 for integration run 1. Purpose: verify the framework
accounts for each stage of real project work explicitly, before the first
integration run — so that run measures phases, not just an end state.*

## Taxonomy

Nine phases. Each row: what the phase produces, its characteristic
failure surface, and how it is verified. A phase without a mechanical
verification story is a phase the framework cannot yet claim to support.

1. **Specification** — goal -> explicit spec: functions, constraints,
   acceptance criteria, required test cases (named). Failure: ambiguity,
   unverifiable criteria. Verification: human review (run 1: human-authored).
2. **Decomposition** — spec -> ordered tasks, each probe-shaped
   (inputs, tool loop, postconditions, completion signal). Failure:
   tasks without postconditions; hidden ordering deps. Verification:
   plan lint (every task has >=1 mechanical postcondition). EXISTS
   (flow YAML).
3. **Scaffolding** — workspace skeleton, deps, package layout. Failure:
   hallucinated dependencies, broken imports, wrong layout. Verification:
   files exist + `python -c "import <pkg>"` exit 0. UNTESTED PHASE —
   axis-1 never exercised it; new evidence surface.
4. **Implementation** — per-task tool-loop execution. The axis-1
   measured seat (gemma 9/9 under production harness, probe v7).
5. **Per-task verification** — three trust layers: (a) mechanical
   postconditions (exists), (b) test execution (NEW: run_check
   predicate), (c) model review (verifier role — recorded, non-blocking
   until measured).
6. **Integration** — cross-task consistency: imports resolve,
   interfaces match, full suite collects and runs. Failure: cross-file
   naming drift — predicted top breaking point (axis-1 tasks were all
   single-file; multi-file consistency is unmeasured). Verification:
   run_check (import + pytest).
7. **System acceptance** — the assembled artifact vs the ORIGINAL
   spec's acceptance criteria, via a headless harness. Verification:
   run_check with expected output (e.g. `echo "!ping" | python -m bot`
   -> "pong").
8. **Repair** — red gate -> diagnosis -> targeted re-task with the
   oracle output (pytest stdout/stderr) delivered VERBATIM in the task
   prompt through the transparent channel. Axis-1 doctrine: corrective
   oracle feedback works (S1); oracle-free reflection does not (S6).
   Integration has oracles; the probe did not. Run 1 mechanism: manual
   re-task from a template (docs/integration/repair-task-template.md);
   automation only after run 1 shows the shape.
9. **Documentation / handoff** — README, run instructions.
   Verification: contains-checks. Low risk; deliberately last.

## Gap summary (what run 1 requires the framework to grow)

- **run_check postcondition predicate** — execute a command in the
  workspace (cwd-pinned, timeout, no network), assert exit code and
  optional stdout-contains. Covers phases 5, 6, 7 with one mechanism.
- **Phase grouping + gate in the flow schema** — tasks carry a `phase`
  field; the runner pauses at phase boundaries; a phase gate = all its
  tasks' postconditions green. Reuses the staged-campaign pause
  discipline on real work.
- **Repair task template** (doc, not code) — failure report + oracle
  output verbatim + the single task it re-opens.
- Verifier task shape — a task whose output is a structured verdict
  (parseable JSON), postcondition = parses. Recorded, non-blocking.

Explicitly NOT built now: automated repair loop-back, spec tooling,
plan lint automation. Each waits for run-1 evidence that it pays.

## Known-friction watchlist (pre-registered, left in deliberately)

- Overwrite guard on revision tasks: phases 4-5 rewrite phase-3 stubs;
  `force` and edit tools are schema-visible. Whether models reach for
  them is a capability measurement, not an obstruction to remove.
- Long tool outputs: pytest tracebacks are the channel's first length
  stress; watch for truncation or salience loss.
- Cross-file naming drift (phase 6) and dependency hallucination
  (phase 3): the two predicted top breaking points.
- Oracle-fed repair (phase 8) is the headline cell: pre-registered
  prediction that it succeeds where S6's oracle-free review failed.

---

## Amendment (2026-07-03): brownfield continuation — extensibility of work

The taxonomy above describes greenfield runs. Real work is mostly
brownfield: extending a completed project. Two additions:

**Phase 0 — state ingestion + re-validation (brownfield entry point).**
Inputs: the project's PROJECT_STATE.md (see template) + the delta spec.
Gate: ALL acceptance/regression gates recorded in the state artifact are
RE-RUN and green before any task executes. The brief is a claim, not
truth; a stale brief is a red gate before work begins. This also turns
every greenfield gate into the brownfield regression suite.

**Phase 9 deepened — handoff is dual-audience.** Human README (as before)
PLUS machine-consumable PROJECT_STATE.md per
docs/integration/project-state-template.md. The context-window
compensation is index + on-demand navigation + re-runnable ground truth
— NOT compression: the model reads the map, pulls only task-relevant
files via read_file, and trusts only re-run gates.

**Brownfield watchlist (pre-registered for run 2):** stale-brief trust
(model acts on a claim phase 0 would have caught), navigation failure
(edits the wrong file despite the map), regression breakage (new work
reds an old gate — detection is free, repair doctrine applies),
convention drift (extension ignores recorded invariants).

Deferred until run-2 evidence: mechanical state extractor
(`agora handoff`: AST signatures, test inventory, green-gate record
from provenance) — replaces model-written FACT sections only; prose
sections stay model-written + verifier-reviewed.
