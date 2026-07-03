# Determinism investigation — findings

*Interpretation of runs_out/determinism-{A,B,B2} (15 runs, gemma-e4b,
probe v4, daemon 0.31.1, temp=0, seed=42, all held fixed; tables in the
executor report of 2026-07-03). Companion: v2 findings §8 (provisional
items this document resolves).*

## What was ruled out

Serialization (B) and forced cold eviction (B2) each failed to produce
byte-identical loop_depth repeats. Combined with the v3.0/v3.0.1 daemon
and prompt identity checks, the eliminated causes are: concurrent-task
scheduling/batching, warm-model state carryover, daemon version, prompt
text, params, world state. What remains is below the request level.

## Mechanism (proposed, with signature match)

**Near-tie greedy decoding decided by GPU-level floating-point
non-determinism** (reduction-order / GEMM algorithm-selection jitter in
the CUDA path). At temperature 0, token choice is argmax over logits;
when two candidates are near-tied, sub-ulp numeric differences between
runs flip the choice. The model's exposure to near-ties is not uniform —
it exists only where the observation underdetermines the output:

- **loop_depth char 19** — the seed_a/seed_b join. The deciding byte
  (seed_a's trailing newline) IS transmitted (`read_file` returns
  `read_text()` verbatim) but at minimum salience: a trailing newline at
  the end of a `[read_file#0]`-decorated message. The model must
  effectively guess the join → near-tie → jitter decides → the observed
  49/53/55B mode ladder (each subsequent newline decision conditions on
  the previous flip). First divergence at char 19 in ALL stages — the
  signature of a fixed near-tie site, not of scheduling noise.
- **force:True variance** — write_file's real, schema-visible `force`
  parameter is semantically irrelevant when out/ is clean; including it
  or not is a low-margin choice. Same mechanism, different site.
- **Everything high-margin is byte-stable**: copy-task content (the
  visible characters dominate), and all trajectory tokens — 15/15
  identical (status, calls, iterations) across every condition.

## Consequences

1. **Trajectory determinism is real and is the reliability substrate.**
   Protocol steps (which tool, what structure, when to stop) are
   high-margin decisions; 15/15 identical trajectories under three
   perturbation regimes. This is what "reliable tool calling" can be
   built on with this stack.
2. **Content byte-determinism at temp=0 is NOT a guarantee** at
   low-salience decision points. temp=0/seed=42 means "deterministic
   except near ties," and near ties live exactly where the harness
   transmits information weakly. Any framework claim of reproducibility
   must be scoped to trajectories, or the tie sites must be removed.
3. **The rendering layer is the actionable variable.** The same defect
   that leaks `[read_file#N]` into copies (v2 findings §8) also
   depresses the salience of byte facts and thereby CREATES the tie at
   char 19. Copy-safe, byte-salient rendering is predicted to fix both.
4. **qwen3 F5 re-read.** "Intrinsic MoE-routing bistability" is demoted
   from sole explanation to amplifier-candidate of this shared
   mechanism: qwen3's near-ties sit at trajectory-relevant sites
   (call-vs-no-call at turn 1), so its flips change trajectories rather
   than bytes; MoE routing plausibly widens the divergence after a flip.
   Checkable from existing v2 data: its first-divergence token position
   across the four small_chain modes (registered as P3 below).
5. **Windows text-mode IO infects byte claims.** `write_text`/`read_text`
   default newline translation (\n→CRLF on write) explains the CRLF in
   captured copies — a harness artifact, not model behavior. Every path
   participating in byte-exactness (write_file, read_file, equality
   predicates, artifact_capture) must pin `newline=''`/binary semantics
   or the equality tasks measure the translation layer.

## Pre-registered predictions for the v5 rendering change

- **P1**: with copy-safe rendering (marker off the content line, content
  verbatim), loop_depth repeat_distinct returns to 1/1 and the written
  concat is byte-correct (the join stops being a guess).
- **P2**: copy tasks become byte-exact (no marker leak; CRLF gone once
  IO newline discipline lands with it).
- **P3**: qwen3's v2 small_chain mode divergence localizes to a single
  early low-margin token position (consistent with the shared
  mechanism); if instead it diverges at many uncorrelated positions,
  the MoE-amplifier story needs revision.
- **Falsifier for the mechanism itself**: if v5 rendering does NOT
  collapse the loop_depth modes, the near-tie-at-the-join account is
  wrong and a lower-level cause (kernel nondeterminism at high-margin
  sites too) must be considered — testable then via CPU inference on
  the single task.

## Status of the v2 §8 provisional items

- "gemma strips newlines" — RESOLVED as: gemma guesses at a
  weakly-transmitted byte under a prompt that raised the stakes of the
  guess; not a stable capability property. The v3.0-vs-later mode
  frequency shift is environment noise on a near-tie, not a behavior
  change.
- gemma remains the leading gate candidate: full protocol, live writes,
  and its only content defects trace to the rendering/IO layer now
  scheduled for v5.
