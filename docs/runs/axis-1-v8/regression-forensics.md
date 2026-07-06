# axis-1 v8 — small_chain regression forensics (v3.2 → v8)

*Read-only. First-divergence via the existing `scripts/p3_qwen3_divergence.py`
turn-signature extractor (`turn_signatures` / `first_divergence`) over each
campaign's `run.log`. Signature = per-turn `(turn, tool_calls, content_len)`.
Both campaigns: probe v7, seed 42, temp 0.0, num_ctx 8192; the ONLY harness
delta is `nudge_budget 1→0` and `review_budget 0→1`. Runs: coder r010–r012,
qwen3 r004–r006.*

## coder-14b small_chain — 3/3 (v3.2) → 0/3 (v8)

| item | value |
|------|-------|
| within-campaign determinism | v3.2 3/3 identical sig; v8 3/3 identical sig (representative r010 each) |
| v3.2 signature | `(1,1,0)(2,0,275)(3,1,0)(4,1,0)(5,1,0)` → PASS |
| v8 signature | `(1,1,0)(2,0,275)` → FAIL |
| **first divergence** | **turn 3** (identical through turn 2; v8 stops, v3.2 continues) |
| model-output position of divergence | **none** — turns 1–2 byte-identical (same `read_file`, same 275-char non-tool turn); divergence is harness-side, after turn 2 |
| turn-2 state (both) | 0 tool calls, output `out/seed_copy.txt` still unwritten |
| v3.2 turn-2 → turn-3 | `completion nudge 1/1` fires → turn 3 `write_file(out/seed_copy.txt, 62 B correct)` → mark_complete → PASS |
| v8 turn-2 → (none) | `nudge_budget=0` → no nudge → loop breaks on the 0-call turn → file never written → FAIL |
| **attribution** | **S2-off mode flip.** Caused by `nudge_budget 1→0`, NOT by S6. The two runs are model-identical up to the divergence; the nudge that rescued coder's turn-2 silent stall at v3.2 is absent at v8. S6 never fired (no valid `mark_complete` reached). |
| mode-flip confirmed? | **Yes** — coder's only v3.2-passing task collapses once the nudge is removed → coder ineligible (see casts note). |

## qwen3:30b small_chain — 2/3 (v3.2) → 0/3 (v8)

| item | value |
|------|-------|
| within-campaign determinism | **none** (MoE bistable): v3.2 sigs 5/3/4-turn across r004/r005/r006; v8 sigs 5/5/3-turn |
| v3.2 rep (r004) | `(1,0,0)(2,1,0)(3,1,0)(4,1,0)(5,0,13)` |
| v8 rep (r004) | `(1,3,0)(2,1,0)(3,1,0)(4,1,0)(5,1,0)` |
| **first divergence** | **turn 1** (`0` vs `3` calls) — but the sequences differ run-to-run within each campaign too, so no stable mode to diverge from |
| v8 failure bytes | turn-1 `write_file` content = `'the exact bytes from seed.txt'` (29 B **instruction-echo**, not the file content); review fired, model re-confirmed 4× unchanged |
| **attribution** | **Unattributable — by construction.** qwen3 is intrinsically non-deterministic at fixed seed/ctx (MoE routing; gate-exempt). Any v3.2-vs-v8 difference is confounded by that bistability regardless of the harness delta. Listed for completeness only. |

## Summary

| task/model | first divergence | cause | attributable to S6? |
|------------|------------------|-------|---------------------|
| coder small_chain | turn 3 (model-identical to turn 2) | nudge on→off (S2 closed) | **No** — S2-off mode flip |
| qwen3 small_chain | turn 1 (no stable mode) | intrinsic MoE bistability | **No** — unattributable by construction |
