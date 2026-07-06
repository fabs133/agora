# Roles and casting — design

*Drafted 2026-07-03 (v8 in flight; casting slots marked conditional).
Purpose: workflows are instantiated from configuration, not hand-wired.
A ROLE is a stable contract; a CAST binds roles to model profiles for a
named hardware envelope, with evidence citations. Same orthogonality
principle as strategy-vs-profile (axis-1 decision log).*

## Role contracts (roles.yaml)

Each role declares: its behavioural contract, the harness configuration
it runs under, and its capability REQUIREMENTS expressed against
capability-matrix columns. Requirements are checkable; a role with no
measured basis yet says `requires: unmeasured` — it can still be cast,
but only with an explicit waiver or a `human` binding.

Initial taxonomy (axis-1 evidence + roster specializations):

| Role | Contract (short) | Evidence basis today |
|---|---|---|
| implementer | agentic tool loop: read/transform/write via write_file, declare via mark_complete; postconditions verifiable | axis-1 gate (v8) |
| verifier | read artifacts + task spec, emit verdict + reasons; NO writes; prefer error surface uncorrelated with implementer | unmeasured (axis-2); S6 review is this role's mechanical precursor |
| planner | decompose goal into probe-shaped tasks with explicit postconditions | unmeasured (axis-2) |
| classifier | label inputs per fine-tune spec | task-specific fine-tunes (roster) |
| retriever | embeddings for search/similarity | nomic-embed-text (roster) |

Cross-model verification note: implementer/verifier SHOULD differ by
model where evidence allows — gemma's residual errors are byte-precision,
instruct's are protocol; uncorrelated error surfaces are what make
review worth running.

## Casts (casts/<envelope>.yaml)

A cast names a hardware envelope (GPU, VRAM budget, residency policy)
and binds each role to a profile from profiles.yaml. Binding fields:
`profile` (or `binding: human`), `resident: bool`, `keep_alive`,
`evidence: {campaign, gate}` or `waiver: <reason>`.

Validation semantics (`agora cast validate <cast>`):
1. Every profile reference resolves in profiles.yaml — unknown fails loudly.
2. Sum of resident model sizes <= vram_budget (sizes from the local
   manifest store, not hand-entered).
3. Every binding either cites matrix evidence rows at a compatible
   (probe_version, harness_hash) satisfying the role's requirements, or
   carries an explicit waiver. Missing both = invalid cast.
4. `binding: human` is always valid and requires no waiver.

Loading semantics (`agora cast load <cast>`): construct the orchestrator
role table; residency plan is advisory to the eviction protocol
(resident models use long keep_alive; non-resident load-on-demand).

## Hardware envelopes

One cast file per envelope. Changing GPUs = writing/choosing a different
cast file; roles.yaml is untouched. The p40-24gb cast exploits the
economics the gate's <=12 GB clause was written for: gemma (9.6) +
instruct (4.7) + nomic (0.3) co-reside with headroom; gemma + qwen3
cannot co-reside at all.

## Benchmark pipeline (Stage 3 — spec, not built)

Goal: pull model -> one command -> vectors in a growing matrix ->
eligibility computed against role contracts.

- `benchmarks/standard-v1.yaml`: named, versioned battery. v1 = the
  tool-call-fidelity probe (current probe_version) under two arms:
  production harness (corrective, review_budget 1) and raw control;
  3 repeats each. Batteries version like probes; battery version is
  part of every vector row's key.
- `agora bench <model-tag>`: auto-roster row (manifest/template/digest
  extraction — the Phase-1 procedure, scripted), generate campaign from
  the battery, run staged, layer2, append vectors.
- Storage doctrine: runs_out JSONL remains the source of truth;
  `capability-matrix.sqlite` is a DERIVED, rebuildable index keyed on
  (model, probe_version, battery, harness_hash, daemon_version, date).
  Comparability enforced at query level: cross-key comparisons require
  explicit opt-in flags, never silent pooling.
- `agora cast eligible <role>`: query the matrix against the role's
  requirements; output candidate profiles with their evidence rows.

## Staging

- Stage 0 (now): this doc + draft roles.yaml + casts/p40-24gb.yaml.
  Acceptance: v8's two pre-committed outcomes each map to a one-line
  cast edit.
- Stage 1 (post-v8): fill conditional bindings; implement cast
  loader+validator (small: schema, three validation rules, loud
  failures). Acceptance: `cast validate` passes on the real cast;
  integration run 1 is instantiated via `cast load`.
- Stage 2: integration run 1 (implementer + human planner only; add
  verifier role only after run 1 is clean — one variable at a time).
- Stage 3: benchmark pipeline per spec above. Acceptance: a never-seen
  model goes pull -> bench -> matrix -> eligibility report with zero
  manual steps besides review.
