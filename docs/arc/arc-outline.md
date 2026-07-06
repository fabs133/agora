# Arc document — outline and theses (draft skeleton, 2026-07-05)

*Working title: "Measuring Without Fooling Yourself: from tool-call
probes to a self-documenting software lifecycle on local models."
Audience: Zenodo preprint + build-in-public artifact. Every claim below
carries a pointer into the committed record (findings Parts 1-16,
F1-F25, session logs, tagged baselines echobot-v1/-v2).*

## Central claims (abstract material)

1. Small local models can carry real multi-phase software work —
   greenfield build, handoff, brownfield extension, re-handoff — IF the
   surrounding framework is audited to the same standard as the models.
   Evidence: the full lifecycle demonstrated on a 9.6 GB model
   (echobot-v2; run 3 brief-as-index navigation 3/3).
2. Of 25 findings across the program, ~21 were framework-, spec-, or
   process-side; TWO were genuine, scoped model boundaries (F14
   whole-file completeness; F18''' reflective-emission) — both dodged
   by task design, not by bigger models.
3. The method is the product: pre-registration, phase gates with
   re-runnable records, provenance-as-truth, conditions-defect
   re-establishment, repair budgets, waiver prohibition.

## Spine

1. **The instrument stack** — pre-registration culture; JSONL truth
   layer; phase gates; the standing rules (conditions-defect
   re-establishment; one-repair budgets; waivers forbidden). Why
   "errors leave evidence" is the only property that survived every
   phase of the program.
2. **Axis-1: measuring tool-call fidelity without fooling yourself.**
   v1 -> A/B strategies (v2) -> the stale-out forensics (live_pass ~ 0;
   every prior pass retracted) -> determinism probe (near-tie greedy
   decoding; trajectory vs content determinism) -> channel transparency
   (v5-v7: marker leak, byte IO, per-result rendering) -> gemma's first
   genuine 9/9. Thesis: the probe spent seven versions removing its own
   lies before it could measure anything.
3. **Harness levers, sorted by failure mode** (the mechanism table):
   S1 corrective errors -> malformed calls (works); S2 nudge -> empty-
   turn stalls (works, narrowly — the erratum story); S6 review ->
   falsified (perception floor); S7 salvage -> scoped negative
   (termination-decided turns are immune). Doctrine: channels, not
   instructions — with the S2 footnote that one instruction-shaped
   mechanism survives for exactly one failure shape.
4. **Integration: the same gate, five causes.** Run 1.x's P5 arc
   (permission -> spec starvation -> contract starvation -> tool
   surface -> affordance void) and the exoneration ledger. F10 (local
   gates), F9 (repair inherits context), F12 (tool surface is evidence).
5. **The two real boundaries and their dodges.** F14 (4/8 whole-file
   vs 7/8 incremental) and F18-family (reflective emission; concrete
   micro-asks at 0 derails). Task design as the capability lever.
6. **Lifecycle: handoff that survives re-execution.** FACT/PROSE split;
   extractor; the F20 re-runnable verification record; phase-0 with a
   red-team; F24 (reuse is not revalidation); brief-as-index
   navigation as the context-window answer.
7. **The audit symmetry** (closing argument): findings moved framework
   -> model -> planner (F15) -> ask-author (F21) -> fact-checker (twice,
   recorded). An instrument that eventually finds defects in its own
   author's spec and its own auditor's probes is the definition of one
   that works.
8. **Limitations & next program**: single model family on the
   implementer seat; verifier fidelity unresolved (instruct series);
   n=1 project shape; Stage-3 benchmark pipeline (tool-surface sweep,
   edit family, forced emission, doc-task axis, envelope scaling) as
   the designed-by-failure battery; capability matrix as the growing
   asset.

## Appendices

A. Findings index F1-F25 (one line each + pointer). B. Runs ledger
(all campaigns, probe versions, tags). C. The standing rules, verbatim.
D. Reproduction pointers (repo paths; everything re-runnable).

## Style rules for the draft

Claims cite the record or don't ship. Numbers come from provenance,
not memory. The exonerations are named as prominently as the findings.
No triumphalism the ledger can't back.
