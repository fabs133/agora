# Pre-registration — F14-at-P4 envelope A/B (2048 vs 4096)

*Registered 2026-07-15, BEFORE execution, per program practice. Binding.
Trigger: the lifecycle baseline (findings Part 17) produced a correct `!roll
NdM` implementation from T4.2 on the **first pass at max_tokens=4096**, where
both recorded 2048 attempts failed it. That is suggestive and nothing more —
this document exists so the question is settled by measurement rather than by
the one run that raised it.*

## The claim under test

**F14 (implementation completeness), P4 clause.** As recorded in Part 8:
gemma-e4b's "weak operation" drops a new feature under a whole-file rewrite —
T4.2 rewrites `core.py` to add `!roll` and loses it. Read as a **capability
floor** of the model at this size, mitigated (not fixed) by task design: small
tasks, tight oracles, incremental builds.

**Competing hypothesis (H1):** the P4 clause is substantially an **output-envelope
artifact**. At `max_tokens=2048` the model cannot emit a complete `core.py`
carrying all four prior commands *plus* `!roll`, so the feature is lost to the
envelope, not to capability. At 4096 the same task, prompt, seed and flow
succeed first try.

**Null (H0):** the envelope is incidental. The 4096 result is sampling noise or
attributable to another variable, and F14's P4 clause stands as written.

## Why the baseline cannot settle it

The baseline changed **four variables at once** versus run 2.0 (Part 17,
"Confounds"): envelope 2048→4096, `salvage_budget` 0→1, Python →3.14.3, and
both-model co-residency. It is also **n=1** on a **non-deterministic** system —
at identical seed/params, a same-day attempt drew a *different* T4.2 defect
(correct output format, wrong input grammar) than run 2.0's (feature dropped
entirely). One sample cannot separate four variables.

**F18' is the standing warning against generalising an envelope result**: raising
gemma's envelope 2048→4096 at T9.2 was *falsified* as an explanation — longer
generation, still empty, `done_reason=stop`. Any envelope effect is therefore
**task-dependent**, and a P4-scoped result must not be read as a global claim.

## Design

**Isolated variable: `max_tokens` only.** Everything else pinned.

| | |
|---|---|
| Task | **T4.2 only** (add `!roll NdM` to an existing `core.py`) |
| Arms | **A: `max_tokens=2048`** · **B: `max_tokens=4096`** |
| n | **3 per arm** (6 executions) |
| Fixed | `temperature=0.0`, `num_ctx=8192`, cast `p40-24gb`, harness `{corrective, nudge 1, review 0, salvage 1}`, flow `integration-run-1-echobot.flow.yaml`, Ollama `0.31.1`, Python `3.14.3`, `OLLAMA_MAX_LOADED_MODELS=2`, model digest `gemma4:e4b c6eb396dbd5992bb` |
| Seed | **varied per repeat** (`42`, `43`, `44`), identical across arms — a fixed seed would measure one sample thrice, not the arm |
| Start state | Identical P4-entry workspace for every repeat: a P3+T4.1 tree (router present, `!roll` absent). Snapshot once, restore before each repeat — **never** reuse a workspace a repeat has touched (F25: a seeded target defeats an overwrite via the write-once guard, and size gates lie) |
| Provenance | Per repeat, record the **effective-params log** (F26 — never the campaign file), plus `iterations`, `tools_used`, `tool_calls_structured`, `turns_reasoning_only`, `salvages_used`, and the artifact |

## Primary outcome (pre-committed, mechanical)

The existing P4 gate check, unmodified:

```
python -c "import random; from echobot.core import handle_message; \
           assert 'rolled 2d6:' in handle_message('!roll 2d6', random.Random(0))"
```

Per repeat: **PASS/FAIL, first pass, no repair.** Arm score = passes / 3.

## Secondary (diagnostic, not decisive)

- `grep -c roll core.py` — feature **absent** (run 2.0's mode) vs **present but
  wrong** (the 2026-07-15 mode). These are different defects and must not be
  pooled.
- Emitted `core.py` byte size vs the 2048/4096 envelope — the direct mechanism
  test for H1. If failures at 2048 truncate near the envelope, H1 gains; if they
  terminate well short of it (`done_reason=stop`), H1 loses and the F18' pattern
  repeats at P4.
- Whether the four prior commands survive (the F12×F14 rewrite-regression shape).

## Decision rule (pre-committed — no post-hoc reading)

- **B ≥ 2/3 and A ≤ 1/3** → H1 supported. **Amend** F14's P4 clause: reclassify
  the P4 feature-drop as *envelope-bounded at 2048*, explicitly scoped to P4 and
  to this model/task; F18''s falsification at T9.2 stands unchanged; the
  capability-floor reading is retired **for P4 only**.
- **A and B within one pass of each other** (e.g. 2/3 vs 3/3, or both ≥2/3) →
  H0. F14's P4 clause **stands as written**; the baseline's first-pass success is
  recorded as sampling, and the envelope is removed from the explanation.
- **Both arms ≤1/3** → neither; the baseline was a lucky draw. F14 stands, and
  the *baseline's* P4 result is annotated as unrepresentative.
- **Any other split** (e.g. A 2/3, B 3/3) → **underpowered, no rewrite.** Record
  the numbers, leave F14 standing, and note that n=3 could not separate the arms.

**F14's P4 clause is NOT rewritten until this runs.** Recorded before execution
so the rule cannot be fitted to the result.

## Threats to validity (recorded up front)

1. **n=3 is small.** It can detect a clean separation (0/3 vs 3/3); it cannot
   resolve a subtle one. The "underpowered" branch above exists so a muddy result
   produces no claim.
2. **Not fully isolated from the baseline.** This A/B holds `salvage_budget=1`,
   Python 3.14.3 and co-residency **fixed at the baseline's values** — it
   isolates the envelope *within that configuration*, and does not license
   claims about run 2.0's other three deltas.
3. **Non-determinism is the substrate.** Varying the seed measures the arm; it
   also means a 3/3 could still be luck. Treat a supported H1 as *evidence*, not
   proof.
4. **P4-scoped by construction.** Says nothing about P5/P7/P9, and explicitly
   nothing about T9.2, where the envelope hypothesis is already falsified.
