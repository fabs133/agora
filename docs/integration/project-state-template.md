# PROJECT_STATE.md — template (v1)

*Purpose: the machine-consumable half of phase-9 handoff. Written at
project completion; consumed (and RE-VALIDATED) by phase 0 of any
extension run. Design doctrine: this file is an INDEX plus CLAIMS —
navigation for small-context models, never a substitute for reading the
actual files, and never trusted without re-running its gates.*

*Sections marked [FACT] must be mechanically checkable against the tree
or gates; sections marked [PROSE] carry intent and are verifier-reviewed.
Every mandatory section header below is gate-checked by contains-checks.*

---

## Identity  [FACT]
Project name, one-line goal, spec document path, completing run id,
git commit at completion, date.

## Architecture & invariants  [PROSE]
The load-bearing decisions and the rules an extension MUST NOT break.
(echobot example: core is a pure function, no IO in core, rng injected,
adapters own all IO.) Each invariant on its own line, imperatively
stated — these are instructions to a future implementer.

## Capability inventory  [FACT]
What the project verifiably does NOW: the named test list (tests are the
behavior documentation) and the acceptance-gate behaviors, stated as
observable input -> output pairs.

## Verification record  [FACT — re-run these in phase 0]
The exact commands that were green at completion, verbatim, one per line:
- pytest -q            (exit 0)
- <acceptance run_check commands exactly as gated>
A future run treats ANY red here as a stop before task one.

## File map  [FACT]
One entry per source file: path — role (one line) — public interface
(signatures verbatim) — imports it exposes/depends on. This is the
navigation index: a task should identify its target files from this
section alone, then read_file only those.

## Conventions  [PROSE]
Naming, layout, test style, error-message style — whatever an extension
must imitate to not read as foreign code.

## Extension points  [PROSE]
Where new work attaches, stated concretely. (echobot example: new
commands register in core's dispatch table + one named test each; new
transports are new adapter modules; core signature is frozen.)

## How to run / test  [FACT]
Verbatim commands for a human or model to run and test the project.
