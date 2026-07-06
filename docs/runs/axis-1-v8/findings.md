# axis-1 v8 — completion review (S6) under probe v7

*Run 2026-07-05. Campaign `campaigns/axis-1-v8.yaml`, output
`runs_out/axis-1-v8/` (r001–r015). Provenance: probe_version **7**,
harness `{tool_errors: corrective, nudge_budget: 0, review_budget: 1}`,
commit `c2a1ebf` (working tree, S6 mechanism added), ollama 0.31.1,
seed 42, temp 0.0, num_ctx 8192. Staged (one model resident at a time),
5 models × 3 repeats.*

Companion to the pre-registration in
[`docs/research/harness-reliability.md`](../../research/harness-reliability.md)
§"v8 pre-registration" and the v3.2 resolution note therein. v8 tests the
S6 completion review against the v3.2 baseline with **one** changed
variable (`review_budget 0→1`); the nudge is OFF (S2 closed).

---

## Headline: the review is observed and ignored — instruct loop_depth 0/3

The pre-registered gate-deciding cell was **instruct loop_depth** (the
trailing-newline off-by-one is the review's declared center-of-mass
target). Prediction: `loop_depth ≥2/3 → instruct 9/9 → GATE MET`.
Falsification clause: *"loop_depth stays 0/3 with the review
observed-and-ignored (confirm-without-fix)."*

**Result: instruct loop_depth 0/3. The review fired 3/3. On all three the
model, shown its own bytes, called `mark_complete` again unchanged —
`post_review_action = confirm`, every time.**

The captured output is 54 bytes:
`apple\napricot\navocado\nblueberry\nblackberry\nboysenberry` — missing
the single trailing `\n` (expected 55). The read-back handed the model
exactly those 54 bytes, prefixed `out/concat.txt (54 bytes):`, and asked
it to confirm or revise. It confirmed. **The reflection surface works
mechanically; the model cannot self-detect a one-byte tail error even
when the bytes are put in front of it.**

> **Falsified exactly as pre-registered.** S6 delivers the information
> (the written bytes, verbatim, through the v7 channel) and the model
> declines to act on it. Reflection without an oracle does not fix a
> failure the model doesn't perceive as a failure.

---

## The grid

| block      | cells   | small_chain | loop_depth | content_robust. | reviews fired | post-review action |
|------------|---------|-------------|------------|-----------------|---------------|--------------------|
| instruct   | 6/9     | **3/3**     | 0/3        | 3/3             | 9/9           | confirm (loop_depth) |
| qwen3      | 0/9     | 0/3         | 0/3        | 0/3             | 6/9           | confirm / None     |
| nemo       | 0/9     | 0/3         | 0/3        | 0/3             | 6/9           | confirm            |
| coder-14b  | 0/9     | 0/3         | 0/3        | 0/3             | 3/9           | confirm (loop_depth) |
| gemma      | **9/9** | 3/3         | 3/3        | 3/3             | 9/9           | other / confirm    |

gemma reproduces its v7 9/9 anchor; **no other profile reaches 9/9**.
(Reviews-fired counts fires across the 3 repeats of all 3 tasks per block,
max 9.)

---

## Did S6 ever earn a pass, or prevent one? No, and no.

The review fires on a **valid `mark_complete` with review budget
remaining**. Across the grid, **not one cell's pass is attributable to
it, and not one passing model was perturbed by it**:

- **gemma (do-no-harm ✓)**: review fired on all 9 of its valid
  completions and gemma stayed **9/9**. After the read-back it either
  went silent (`other`) or re-confirmed — never corrupted an
  already-correct file. The mechanism does not perturb a passing model.
- **instruct loop_depth**: fired 3/3, `confirm` 3/3, still 0/3 —
  observed-and-ignored on the trailing-newline error.
- **nemo**: fired on small_chain + loop_depth, `confirm`, still 0/9. The
  read-back changed nothing.
- **coder**: fired only on loop_depth (3/3, `confirm`, still fails).
  small_chain and content_robustness never reached a valid
  `mark_complete`, so the review was **structurally unreachable** there
  (fired 0/3 each).
- **qwen3**: categorical/bistable, gate-exempt; no pass traceable to it.

**S6 is either observed-and-ignored (the model confirms wrong bytes) or
unreachable (no valid `mark_complete` to fire on).** Neither failure mode
is a harness bug — they are honest limits of no-oracle reflection.

---

## Structural blind spot: content_robustness review has no runway (all profiles)

`post_review_action` is **`None` for content_robustness in every one of
the 5 blocks.** The redirect stage's `max_iterations` is **4**, and every
model spends all four turns (read → read → write → `mark_complete`) before
completing. The review fires *on* turn 4, so the loop hits the iteration
cap before the model gets a turn to consume the read-back. **The review is
inert on content_robustness by iteration-cap construction** — a second
by-construction blind spot alongside the pre-registered coder-omission one.
Any future read-back lever must leave a spare turn after `mark_complete`,
or the reflection is delivered into a closed loop.

---

## Byte-level failure taxonomy (first repeat per block)

Under v7 these are genuine model byte errors, now each shown its own
read-back and left unchanged:

| task | expected | instruct | qwen3 | nemo | coder | gemma |
|------|----------|----------|-------|------|-------|-------|
| small_chain (62 B) | 62 ✓ | 62 ✓ | **29** wrote the *instruction phrase* | **61** (−`\n`) | **unwritten** (no `mark_complete`) | 62 ✓ |
| loop_depth (55 B) | 55 ✓ | **54** (−`\n`) | unwritten (bistable) | **21** (dropped operand b) | **56** (double `\n` at join) | 55 ✓ |
| content_robust. (50 B) | 50 ✓ | 50 ✓ | **49** (−`\n`) | **51** literal `\n` | unwritten | 50 ✓ |

- **trailing-newline off-by-one** — instruct loop_depth, nemo small_chain,
  qwen3 content_robustness. The dominant error, and the one S6 targeted;
  confirmed-without-fix wherever the review fired on it.
- **dropped operand** — nemo loop_depth (21 B, seed_b never concatenated).
- **spurious separator** — coder loop_depth (`avocado\n\nblueberry`, 56 B).
- **literal escape** — nemo content_robustness (backslash-`n`, 51 B),
  the lone escaping holdout under v7.
- **instruction-echo** — qwen3 small_chain wrote the literal string
  `the exact bytes from seed.txt` (29 B) instead of the file content.

**Coder deviation, flagged not celebrated:** coder small_chain scored
**0/3** here vs its v3.2 3/3. The review fired **0/3** on that cell (no
valid `mark_complete`), so S6 cannot be the cause — the file was simply
never written (`prose_no_call`). Unexplained by this lever; needs the
transcript, not attributed to the review.

---

## Integration gate

Gate (harness-reliability.md): **≥2 profiles at 9/9, ≥1 of them ≤12 GB.**

Under the v8 config, **gemma-e4b is the only 9/9** (≤12 GB ✓). No second
profile reaches 9/9 — instruct 6/9, and qwen3 / nemo / coder all 0/9.
**The gate is NOT met**, unchanged from v3.2. S6 did not lift a second
model across the line: on the one cell it was designed for (instruct
loop_depth) it was observed-and-ignored.

---

## Stopping rule — this phase is closed

S6 was the **final registered lever** of this phase (pre-committed: *"after
v8 the gate is evaluated with whatever pool exists — met, or formally
renegotiated. No v9 lever."*).

- **Gate met?** No.
- **Therefore:** the decision goes to the project owner —
  **single-profile pool (gemma) + qwen3 ensemble-optional, vs. gate
  revision** — with this full grid attached. No further harness levers are
  pre-registered; the residual is a model-fidelity limit (one-byte tail
  errors the models won't self-correct even when shown), not a
  harness-recoverable class.

What v8 does settle, cleanly: the S6 read-back is **production-valid and
do-no-harm** (gemma 9/9, unperturbed; the mechanism constructs nothing at
`review_budget=0`), it is **correctly targeted** at the dominant post-v7
residual (trailing-newline off-by-one), and it **still fails to move it** —
because the binding constraint is the model's byte-level self-perception,
which no in-loop reflection surface reaches.
