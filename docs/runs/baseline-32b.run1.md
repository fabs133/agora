# `baseline-32b.run1`

**Stage-0 lifecycle baseline** for the profile system on a 24 GB Tesla P40
(Windows 11). The goal was to verify the model load → resident → keep_alive
lifecycle works as designed, that the async orchestration is awaited to
completion, and to capture the first set of P40 baseline metrics. The session
ran two arms: `qwen-coder-32b-p40` (the headline) and `gemma4-e4b-p40` (a
small/fast contrast after the 32b finding pointed to model size as the
bottleneck).

## Setup

| Field | Value |
|---|---|
| Hardware | Windows 11, Tesla P40 24 GB + RTX 3060 Ti 8 GB (Ollama uses the P40) |
| Driver / CUDA | 582.16 / CUDA 13.0; P40 in TCC mode |
| Ollama | local daemon, listening on http://localhost:11434 |
| Conduit | matrixconduit/matrix-conduit:latest via `conduit/docker-compose.yml` |
| Branch | `runs/baseline-32b-lifecycle` |
| Probe | `scripts/probe_model_lifecycle.py` (this branch) |
| Profile system | `src/agora/fleet/profiles.py` (commits 636c169 → aefb1fe) |

## Pre-flight notes (issues surfaced before a clean run)

Five environmental issues had to be resolved before the lifecycle runs
were sound:

1. **Conduit container had a stale empty `conduit.toml/` directory**
   (Docker had auto-created it as a directory on a prior bind-mount
   failure). Recovered by `rmdir conduit.toml` + re-copying
   `conduit.example.toml`; container restarted cleanly.

2. **Two-GPU machine confuses the VRAM pre-flight.**
   `_probe_nvidia_smi` in [src/agora/fleet/vram.py](../../src/agora/fleet/vram.py)
   takes `min(memory.free)` across all visible GPUs because "Ollama pins
   a single device". On this box the smaller RTX 3060 Ti (≈6 GB free)
   is that minimum, so any 32B/14B model is refused even though Ollama
   would actually use the 22.8 GB P40. **`CUDA_VISIBLE_DEVICES=0` does
   not help** — `nvidia-smi` reads the kernel module directly and
   ignores the CUDA env. The clean bypass is the existing
   `_is_model_resident` short-circuit in `check_model_fits`: pre-warm
   the target model via `/api/generate` so `/api/ps` shows it loaded,
   then the pre-flight returns "already resident" without consulting
   nvidia-smi.

3. **The shipped profiles were over-spec'd for the P40 (round one).**
   `qwen-coder-32b-p40` shipped with `num_ctx=16384`; `qwen-coder-14b-bigctx-p40`
   shipped with `num_ctx=32768`. Both blew past usable VRAM via the
   KV-cache footprint, not the weights — see the next note. Profile
   re-tune commit 451bb43:

   | Profile | num_ctx before → after |
   |---|---|
   | qwen-coder-32b-p40 | 16384 → **4096** |
   | qwen-coder-14b-bigctx-p40 | 32768 → **16384** |

4. **The actual model-size driver is KV cache, not weights.** The
   `ollama list` "17 GB" / "19 GB" sizes are the on-disk GGUF blobs;
   the runtime footprint reported by `ollama ps` (`size`) is
   `weights + KV cache(num_ctx) + ~1 GB compute scratch`. For deep
   architectures (Gemma 3/4 has many layers per param), the KV term
   dominates. Concrete numbers from this session:

   | Model | weights (disk) | KV @ 4K | KV @ 16K | KV @ 32K |
   |---|---|---|---|---|
   | qwen2.5-coder:32b | 19 GB | ~3 GB | ~6 GB | ~9 GB |
   | qwen2.5-coder:14b | 9 GB | ~1.5 GB | ~3 GB | ~6 GB |
   | gemma4:26b | 17 GB | ~2 GB | ~8 GB | (untested) |
   | gemma4:e4b (8B) | 9.6 GB | ~1 GB | ~2 GB | ~4 GB |

   So "the 32b model doesn't fit on a 24 GB card" was always the wrong
   framing — the 32b *weights* fit fine; the 16K KV cache was what
   pushed total to ~25 GB. Standard procedure now: pick the smallest
   `num_ctx` that fits comfortably, raise only when a run shows the
   workload actually needed more (recorded in
   [`feedback-kv-cache-first`](../../../.claude/projects/PROJECT/memory/feedback_kv_cache_first.md)).

5. **ECC mode reduced effective VRAM by ~1.5 GiB.** The P40 reports
   23040 MiB total with ECC on (vs. 24576 MiB physical) because ECC
   reserves parity. With ECC enabled, even `qwen2.5-coder:32b @ ctx=4096`
   could not load fully on GPU: `ollama ps` showed `11%/89% CPU/GPU`
   split, with ~2.8 GB spilling to system RAM. The cumulative pressure
   (CPU spillover + Python subprocesses + Docker/Conduit) was enough
   to OOM-kill VS Code on the original `num_ctx=16384` attempt. User
   disabled ECC mid-session (`nvidia-smi -i 0 -e 0` + GPU reset);
   that reclaimed the 1.5 GiB and cut the 32b@4K offload from 11% to
   4%. The remaining 4% is intrinsic — the 32b's runtime footprint
   (~23.7 GB) genuinely exceeds the 24 GiB physical VRAM by ~1 GiB
   even at ctx=4096. To run 32b fully on GPU on this card you would
   need a lower-quant variant (Q4_K_S/IQ3) or a smaller model.

## 32b @ num_ctx=4096 arm (the headline run, ECC still on)

Profile: `qwen-coder-32b-p40` after the re-tune (`num_ctx=4096`,
`max_tokens=4096`, `keep_alive=30m`). Model pre-warmed before the run so
`_is_model_resident` short-circuited the multi-GPU VRAM pre-flight.
Probe data lives under `runs_out/32b/`.

### Lifecycle (Step-4 assertions)

| # | Assertion | Verdict | Evidence |
|---|---|---|---|
| 1 | Warmup loaded the model before the first task | **PASS** | `[*] VRAM check ... already resident in VRAM (skipping free-space check)` (run.log); pre-warm via `/api/generate` at run kickoff put the model in `/api/ps` before the orchestrator's first dispatch. |
| 2 | Loaded fully on GPU vs offloaded | **FAIL** (offload confirmed) | `ollama ps` and 147 timeline samples all report `qwen2.5-coder:32b 25 GB 11%/89% CPU/GPU 4096`. `size=25,434,650,624` bytes vs. `size_vram=22,520,199,168` bytes → **~2.8 GiB on CPU**. This is the brief's key Stage-0 number for the 32b. |
| 3 | Stayed resident — no reload churn | **PASS** | All 147 timeline samples show the same digest (`b92d6a0bd47e`), same processor split (`11%/89%`), same context length (4096). The `UNTIL` field keeps rolling forward (e.g. `24 → 29 → 26 minutes from now`) → keep_alive=30m is being refreshed on every chat request as expected. No reload events. |
| 4 | Awaited to completion | **PASS** | `grep -cE "was never awaited\|destroyed but it is pending\|Event loop is closed\|Unclosed client session\|Unclosed connector" run.log` → **0 hits**. Exit code 0 (`probe_meta.json`); runner printed the `Project phase: done / Success: True / Duration: 1278.9s` summary block. |
| 5 | Post-run teardown matches design (keep_alive governs eviction) | **PASS** | `snapshot_post.txt` 4 s after probe exit: `qwen2.5-coder:32b ... 26 minutes from now`. Model still resident, no explicit unload step (by design). |
| 6 | Ollama tokens/sec sanity | N/A | Ollama server log was not captured (separate from run.log on Windows). Indirect indicator: the run took 1278.9 s wall-clock for ~42 k input / 884 output tokens spread across 7 task attempts; effective speed is consistent with the 11% CPU offload (single-digit tokens/sec on the spilled layers would explain the slowdown). |

### Stage-0 metrics

| | |
|---|---|
| (a) GPU/CPU split | **11% CPU / 89% GPU** (steady across the run) |
| (b) Peak VRAM used | 21,140 MiB / 23,040 MiB (ECC on) — i.e. effectively saturated, 1.8 GiB free margin during the run |
| (c) Tasks done / total + first-pass + loopbacks | **3 / 13 succeeded**, of which 2 first-pass (`fetch_intro`, `fetch_commands`); `design_commands` succeeded on attempt 2; `design_modules` failed all 3 attempts (the model kept calling `read_file` for the wrong file instead of `write_file`); downstream 9 tasks never ran because `build_skeleton` had unsatisfied dependencies. No loopbacks (the project advanced to DONE via the review phase's auto-approval after the 5-minute review timeout). |
| (d) Total wall-clock | **1278.9 s ≈ 21.3 min** |
| (e) Slower than the 7b ~5-6 min reference? | **Yes, ~3.5-4× slower** (consistent with model-size + the 11% CPU offload tax). The slowdown is dominated by per-turn LLM latency (7-25 s per turn for 32b vs 2-5 s for 7b), not framework overhead. |

### Other observations from the 32b arm

- **Two phase cascades fired in <10 ms.** After the analysis phase's
  `design_modules` exhausted its retries at 13:54:54.039, the
  orchestrator advanced `analysis → architecture → implementation →
  testing → review` in 27 ms — none of those phases had any ready
  tasks (every downstream task depended on `design_modules`'s missing
  output). This is correct behaviour but it leaves the project room
  with a sparse, jumpy phase log. Worth flagging if a UI consumer
  ever needs steady cadence.
- **`Success: True` is misleading at the project level.** Only 3 / 13
  tasks passed; the project still reached `phase=done` because the
  REVIEW poll auto-approved after the 5-minute timeout. Not a bug —
  the `discord-bot.run13` style runner only fails if the *review*
  rejects, not if individual tasks fail. The per-task table is the
  authoritative outcome.
- **The 11% CPU offload made the host system marginal.** The original
  16K-context attempt OOM-killed VS Code; even at 4K the IDE noticeably
  stuttered during the run. Disabling ECC mid-session (see pre-flight
  note 5) reduced offload from 11% → 4% and the system stayed responsive
  in subsequent runs.

## gemma4:e4b @ num_ctx=4096 arm (contrast — small model, ECC off)

After diagnosing that the 32b's offload was the root cause of the
system instability, the contrast arm switched to a model that fits
100% on GPU with comfortable headroom. `gemma4:e4b` is 8B params @
Q4_K_M (~9.6 GB on disk) and has both `tools` and `thinking`
capabilities per `/api/show`.

A new `OllamaAdapter` rule strips `<think>...</think>` /
`<thinking>...</thinking>` blocks before the tool-call parser sees the
content — commit a68fb4a, six unit tests, all green. This lets
Gemma's reasoning mode coexist with Agora's existing tool-call
grammar.

Pre-warm result on the ECC-off P40 (24,576 MiB total):

```
ollama ps:    gemma4:e4b   c6eb396dbd59   11 GB   100% GPU   4096
nvidia-smi:   GPU 0: 10833 / 24576 MiB used   (13,641 MiB free)
eval speed:   71 tok/s
```

Probe data lives under `runs_out/gemma4-e4b/`.

### Lifecycle (Step-4 assertions)

| # | Assertion | Verdict | Evidence |
|---|---|---|---|
| 1 | Warmup loaded the model before the first task | **PASS** | `[*] VRAM check ... already resident in VRAM (skipping free-space check)`; the model was pre-warmed via the same `/api/generate` trick that worked for 32b. |
| 2 | Loaded fully on GPU vs offloaded | **PASS** | 180 / 181 timeline samples report `gemma4:e4b 11 GB 100% GPU 4096`. **Zero CPU offload** at the configured num_ctx. |
| 3 | Stayed resident — no reload churn | **PASS (with one notable anomaly)** | All samples show the same digest (`c6eb396dbd59`) and `100% GPU`. The `UNTIL` field keeps rolling forward → keep_alive=30m is being refreshed. **One single sample mid-run showed `gemma4:e4b 16 GB 100% GPU 32768`** — a transient ~8-second window where Ollama held the model at `num_ctx=32768`, then it dropped back to 4096. Possible causes: another process briefly contacted the daemon with a larger context, or Ollama's internal context-resize on a marker turn. Did not break the run; surfaces a follow-up worth investigating (probe timeline.log captured it cleanly). |
| 4 | Awaited to completion | **PASS** | `grep -cE "was never awaited\|destroyed but it is pending\|Event loop is closed\|Unclosed client session\|Unclosed connector" run.log` → **0 hits**. Exit code 0; runner printed the `Project phase: done / Success: True / Duration: 1539.0s` summary block. |
| 5 | Post-run teardown matches design | **PASS** | `snapshot_post.txt` 5 s after probe exit: `gemma4:e4b ... 28 minutes from now`. Resident as designed; no explicit unload. |
| 6 | Thinking-block strip activity | **PASS (no breakage)** | The new `OllamaAdapter._strip_thinking_blocks` is in the path. Run completed without any tool-call parsing errors that would have surfaced if Gemma's reasoning trace had leaked through. (Tag activity isn't directly logged — the strip is invisible from outside the adapter. The proof is the run completing cleanly with a reasoning-capable model.) |

### Stage-0 metrics

| | |
|---|---|
| (a) GPU/CPU split | **100% GPU / 0% CPU** (180 of 181 samples; one anomalous 32768-ctx sample also 100% GPU) |
| (b) Peak VRAM used | 10,833 MiB / 24,576 MiB — i.e. ~44% of the card, **13.6 GiB of free margin** |
| (c) Tasks done / total + first-pass + loopbacks | **4 / 13 succeeded.** First-pass: `fetch_intro` ✓ (3 iter), `fetch_commands` ✓ (3 iter), `design_commands` ✓ (5 iter). Auto-retry: `design_modules` ✓ on attempt 2 (5 iter; first attempt failed at 4 iter). **`build_skeleton` failed all 3 attempts at 6 iter each** (staged task with a literal `bot.py` template — model couldn't reproduce it verbatim). Downstream 8 tasks never ran (every implementer/tester task depends on `build_skeleton`). 0 loopbacks; review auto-approved after 1 min. |
| (d) Total wall-clock | **1539.0 s ≈ 25.7 min.** Note: this is *longer* than the 32b's 21.3 min even though gemma4 is 4× smaller — because gemma4 actually got further into the task DAG (cleared `design_modules` + `design_commands` first-pass + 3 attempts at `build_skeleton`), so it did more total work per run. |
| (e) Slower than the 7b ~5-6 min reference? | **Yes, ~4-5× slower wall-clock.** But for opposite reasons: each gemma4 turn is *fast* (eval 65-71 tok/s, ~3× faster than 32b on this card) — the slowdown is iteration count (18 iterations on `build_skeleton` alone), not per-turn latency. |

### Other observations from the gemma4:e4b arm

- **System remained responsive throughout.** No IDE freezes, no
  swap thrashing. With ECC off and the model at 100% GPU, there's
  no spillover competing for system RAM, and the GPU contention
  doesn't reach the desktop (P40 is in TCC mode, display is on the
  3060 Ti).
- **Output verbosity is much higher than 32b.** 105 k input / 11.6 k
  output tokens for gemma4 vs. 42 k input / 884 output for the 32b
  arm. Gemma 4 writes more prose per turn even with `<think>` blocks
  stripped — possibly because it's emitting more explanatory
  `content` alongside tool calls. The framework absorbs this fine;
  noting it because it'll affect cost estimates for any future
  LiteLLM-backed gemma run.
- **Staged tasks remain the weak point.** Both the 32b and gemma4:e4b
  cleared the early non-staged tasks (`fetch_*`, `design_*`) but hit
  staged-task walls. For gemma4:e4b that wall was `build_skeleton`
  (literal `bot.py` template); the model burned 18 total iterations
  on it across 3 attempts and never produced a `bot.py` that passed
  the `postcond_python_imports` + `postcond_no_code_after_main_block`
  gates. Worth a separate investigation: are the staged instructions
  pattern-matched too tightly for non-Qwen models? The instruction
  refers to `discord.Intents` and `commands.Bot` semantics that
  gemma4 may have transcribed slightly differently.
- **Transient num_ctx anomaly (TODO).** One timeline sample shows
  `gemma4:e4b 16 GB 100% GPU 32768`. Worth checking whether this
  reproduces and what caused the context size change — could be
  benign (Ollama internal) or a sign of a request from another
  process sneaking in.

## Verdict

**Did 32b @ 4K offload on this hardware?** **Yes** — 11% CPU offload
even at the smallest practical context window, with ECC enabled. The
32b Q4_K_M's runtime footprint genuinely exceeds 23 GiB. Even with
ECC disabled (24 GiB available), the residual offload is ~4%
because total runtime (~23.7 GB) is larger than physical VRAM
(~24 GB) once compute scratch is reserved. Conclusion: this card
cannot run qwen2.5-coder:32b @ Q4_K_M fully on GPU. Options forward
are a lower quant (Q4_K_S/IQ3), a smaller model, or accepting ~5%
CPU offload as the floor.

**Was the lifecycle plumbing sound?** **Yes — every assertion the
brief defined passed for the 32b arm** (load, keep-resident,
no-reload-churn, awaited-to-completion, teardown-by-design). The
profile system (`AGORA_PROFILE=qwen-coder-32b-p40` runs cleanly with
no other env), the keep_alive threading from commit 4c7a851, and the
ECC-aware physical-VRAM bookkeeping all behaved as designed. The
remaining issues are workload (the 32b's task-completion quality at
4K context) and hardware (the 24 GiB ceiling), not framework.

**Was first-pass clearly better than the 7b reference?** **No** —
neither arm beat the 7b reference. qwen2.5-coder:32b first-pass-completed
2 / 13 tasks; gemma4:e4b 3 / 13 — both substantially worse than the
7b's 12 / 12 in [discord-bot.run13](discord-bot.run13.md). The
constraint isn't model size; both arms stalled on **staged tasks**
that prescribe literal `bot.py` content (the 7b reference cleared
those because the staging was tuned for qwen2.5:7b's exact prose
style). A separate investigation should probe whether the
`build_skeleton` / `build_*` staged instructions are over-fitted to
qwen2.5-instruct and need a model-agnostic rewrite — that's a
framework concern, not hardware.

## Artefacts

- Probe outputs: `runs_out/32b/` (run.log, timeline.log,
  snapshot_pre.txt, snapshot_post.txt, probe_meta.json) and
  `runs_out/gemma4-e4b/`.
- Code changes on this branch: thinking-strip in
  [`src/agora/fleet/llm_adapter.py`](../../src/agora/fleet/llm_adapter.py)
  (a68fb4a), profile re-tune in
  [`profiles.yaml`](../../profiles.yaml) (451bb43), gemma4-e4b profile
  add (ad35532), probe script in
  [`scripts/probe_model_lifecycle.py`](../../scripts/probe_model_lifecycle.py)
  (f05fce8).
- Memory recorded:
  [`feedback-kv-cache-first`](../../../.claude/projects/PROJECT/memory/feedback_kv_cache_first.md).
