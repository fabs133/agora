# Repair task template (integration run 1, phase 8)

*Doc, not code. Phase 8 of docs/design/project-phases.md: on a RED phase gate,
a human (run 1) hand-assembles ONE re-task from this skeleton and re-runs the
single failed gate. Repair budget = 1 per gate; a second red on the same gate
stops the run for a chat-side decision. Automation waits until run 1 shows the
shape (project-phases §"Explicitly NOT built now").*

Doctrine: repair works because the oracle output is delivered VERBATIM through
the transparent channel — axis-1 S1 (corrective oracle feedback works) vs S6
(oracle-free reflection does not). Integration HAS oracles (pytest, run_check);
the probe did not. Deliver the oracle, do not paraphrase it.

---

## Skeleton

```
{original task text, verbatim — the same instruction the task first ran with}

The following gate failed:

    {the single failing gate, named — e.g. "P5 gate: run_check `pytest -q`"}

The failing tests/spec below are AUTHORITATIVE. Your artifact violates them.
Modify your artifact; do not dismiss the failures.
Rewrite the file with write_file using force=true — the file exists and must
be replaced.

Oracle output (verbatim):

    {the run_check record's stdout/stderr for the failed command, copied
     byte-for-byte from phases.jsonl / tasks.jsonl run_check_records —
     NOT summarized, NOT truncated beyond the 4 KB capture bound. If it was
     truncated (stdout_truncated=true), say so and attach the fuller log path.}

Re-satisfy exactly this gate. Change only what the oracle points at; do not
touch files outside {the task's declared output_path(s)}. When done, the gate
    {the exact gate command, verbatim}
must pass.
```

## Assembly rules

1. **One gate, one task.** Re-open only the task whose phase gate went red.
   Do not bundle unrelated fixes; a repair that touches other phases
   invalidates their green gates.
2. **Oracle verbatim.** Paste the captured `run_check` stdout/stderr from
   provenance unchanged. The whole point (S1) is that the model repairs from
   the real error, not a human's gloss of it.
3. **Name the single gate to re-satisfy** and quote its command exactly, so
   re-running is unambiguous.
4. **Scope the edit** to the task's declared `output_path`(s). Repair is not a
   redesign.
5. **Budget 1.** Second red on the same gate after a repair → stop, record both
   oracle outputs, escalate to a chat-side decision. Do not spend a third turn.
6. **Authority clause (F9).** The oracle block is preceded by an explicit
   authority statement — the tests/spec are authoritative, the artifact is what
   changes. Run 1.2's cross-phase repair re-read a context-starved description,
   found its drifted file consistent with it, and no-op'd; a repair prompt must
   name the failing signal as the thing to obey, not weigh. (The authoritative
   CONTRACT itself rides in via the original task text, which now carries the
   signature inline — repair inherits it.)

## Provenance to pull the oracle from

- `phases.jsonl` — the red `PhaseGateRecord` (phase, blockers, per-task
  per-predicate outcomes) identifies WHICH task and gate failed.
- `tasks.jsonl` — the failed task's `run_check_records` carry the verbatim
  `stdout`/`stderr` (bounded 4 KB each; `stdout_truncated`/`stderr_truncated`
  flag when the real output was longer — the long-output watchlist item).
