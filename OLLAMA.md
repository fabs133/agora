# Ollama operations — this device

> **Scope: the author's reference box — NOT general guidance.** Every value
> below (model directory, GPU UUIDs, port, co-residency budget) is specific to
> one machine. Nothing here is a requirement for running Agora elsewhere; a
> fresh install with a default `ollama serve` is the normal case. `docs/SETUP.md`
> is the portable path. Read this file as a worked example of *what a cast's
> hardware envelope looks like in practice*, not as setup steps.

Operational reference for driving Ollama on this machine: **Windows 11,
dual-GPU** (GPU 0 = Tesla P40 24 GB, GPU 1 = RTX 3060 Ti 8 GB). The P40 was
added ~2026-05-25; it is the box the axis-1 characterization campaign, the
echobot integration runs, and the 2026-07-15 lifecycle baseline ran on. Work
predating the upgrade (the Round-18 stress-test era, ~April 2026) ran on the
3060 Ti alone.

## 1. Start the daemon

Ollama is **not** running by default here. Start a **bare `serve`** with the
environment set inline — do not rely on the tray app / scheduled task (see
[Gotchas](#6-gotchas-on-this-box)):

```bash
# multi-seat casts (p40-24gb: implementer+tester gemma-e4b, verifier instruct):
OLLAMA_MODELS='D:\ollama\models' OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=2 ollama serve
```

## 2. Required environment

| Var | Value | Why |
|---|---|---|
| `OLLAMA_MODELS` | `D:\ollama\models` | The models live here. The default (`<home>\.ollama\models`) is **empty** — omit this and every model looks "missing." |
| `OLLAMA_NUM_PARALLEL` | `1` | `>1` multiplies KV-cache VRAM and caused intermittent `GGML_ASSERT(mem_buffer != NULL)` crashes (~11% of tasks) on the P40. |
| `OLLAMA_MAX_LOADED_MODELS` | **`2`** for the `p40-24gb` cast; `1` only when a single seat loads a *large* model | **Size the cap to the cast, not to the box.** The old blanket `1` ("24 GB can't hold two of the larger models") is true only of the *large* pairings (e.g. two 32b-class models). The validated `p40-24gb` cast needs gemma-e4b (9.6 GB) **+** instruct (4.7 GB) ≈ **14.6 GB co-resident, which fits 24 GB comfortably**. At `1`, every verifier task evicts gemma and the next phase reloads 9.6 GB from disk — pure wall-clock loss, no correctness effect. The 2026-07-15 lifecycle baseline ran at `2`. |

> **Prewarm caveat.** A cast prewarm loads models at Ollama's **default context
> (32768)**, ignoring the profile's pinned `num_ctx`; the first real task call
> then reloads the model at the pinned value (e.g. 8192). Cost is a redundant
> load, not a fidelity loss — but `ollama ps`'s CONTEXT column reads 32768 until
> that first call lands, so check it *after* work starts, not before.

## 3. GPU pinning

Two GPUs are present. Pin work to the **P40** (24 GB, the campaign GPU) by UUID —
stable across reboots, unlike indices:

```bash
CUDA_VISIBLE_DEVICES=GPU-5ccbe0fc-ad04-8f9e-0193-ada20d7dba30   # Tesla P40 (index 0 also works)
```

| Index | GPU | VRAM | UUID |
|---|---|---|---|
| 0 | Tesla P40 | 24 GB | `GPU-5ccbe0fc-ad04-8f9e-0193-ada20d7dba30` |
| 1 | RTX 3060 Ti | 8 GB | `GPU-0c9a6062-2512-ad5d-4477-33450ce9ec8e` |

## 4. Health / status

```bash
curl -s http://localhost:11434/api/version   # is it up?
curl -s http://localhost:11434/api/tags      # local models
curl -s http://localhost:11434/api/ps        # what's resident + CONTEXT + keep-alive
```

The `ollama ps` **CONTEXT** column is the check for the prewarm bug: it should
read your pinned `num_ctx` (e.g. `8192`), not the model default `32768`.

## 5. Load / evict a model

```bash
# prewarm AT the pinned context (bare prewarm loads the model's default 32768):
curl -s http://localhost:11434/api/generate \
  -d '{"model":"gemma4:e4b","keep_alive":"30m","options":{"num_ctx":8192}}'

# evict immediately:
curl -s http://localhost:11434/api/generate \
  -d '{"model":"gemma4:e4b","keep_alive":0}'
```

The campaign harness (`scripts/run_campaign.py`) does this automatically via its
eviction protocol. Commit `f037901` (`fix/prewarm-num-ctx`, merged to `main`;
cited as `55de631` before the 2026-07 history rewrite) is what makes **that**
prewarm carry `num_ctx` — without it, each model block's first run loads at 32768
and is not comparable to its steady-state repeats. Confirmed live on daemon
`0.31.1`: the harness logs `evicted → pre-warmed gemma4:e4b` and `/api/ps` then
reads `context_length=8192`.

> **The fix is scoped to the campaign harness — it does NOT cover the
> orchestrator.** `agora.fleet.vram.warmup()` (the path used by
> `scripts/run_phased.py`, the demo, and the `agora` CLI — i.e. everything that
> builds an orchestrator) posts `options: {"num_predict": 1}` with **no
> `num_ctx`**, so Ollama loads the model at its default 32768. Observed live in
> the 2026-07-15 lifecycle baseline: `ollama ps` read `ctx=32768` for both cast
> models at warm-up, then `8192` for gemma once the first real task call landed
> (the adapter *does* pin `num_ctx` per call, which reloads the model at the
> pinned value). Net cost on that path is a **redundant model load in
> wall-clock, not a fidelity loss** — the generations themselves ran at 8192.
> For campaign-style A/B comparisons the distinction matters; for a phased run
> it is latency only. Fixing `warmup()` to accept and forward `num_ctx` is
> recorded, not yet done.

## 6. Gotchas on this box

- **Tray app / `Ollama-Server` scheduled task respawns `ollama` on :11434 with
  the wrong config** (default models dir, multi-load), which blocks a manual
  `serve` from binding. That scheduled task is currently left **disabled**. If a
  `serve` fails to bind with "address in use," a stray tray instance is already
  up — kill it first, or use the running instance only if its `/api/tags` shows
  the full 12 models.
- **A `serve` started from a background shell dies when that shell/session is
  torn down.** Re-run the start command when you come back. **The same applies to
  the Conduit container** — a Docker Desktop started from a torn-down session
  takes the container with it, and `run_phased` then hangs on the Matrix client
  (see `docs/SETUP.md` troubleshooting). Re-check `agora doctor` after any
  session break, before blaming a run.
- **Port 11434 can be unbindable even with nothing listening on it.** Windows
  reserves dynamic port ranges for WinNAT/Hyper-V, and Docker Desktop's presence
  can put **11434 inside a reserved block** — `ollama serve` then dies with
  `bind: An attempt was made to access a socket in a way forbidden by its access
  permissions`, while `netstat` shows the port free. Diagnose:

  ```bash
  netsh interface ipv4 show excludedportrange protocol=tcp   # is 11434 inside a range?
  ```

  Observed on this box 2026-07-15: reserved range **11420–11519** swallowed
  11434. Two ways out — (a) elevated: reserve the port back
  (`netsh int ipv4 add excludedportrange protocol=tcp startport=11434
  numberofports=1`) or bounce `winnat`; (b) **no elevation needed** — move the
  daemon and point Agora at it:

  ```bash
  OLLAMA_HOST=127.0.0.1:11700 ... ollama serve      # pick a port above the reserved blocks
  # .env:
  AGORA_OLLAMA_BASE_URL=http://localhost:11700
  ```

  The lifecycle baseline ran this way. One `.env` line absorbed it with no code
  change — the single-source config design earning its keep.
- **Version drift:** the daemon is now `0.31.1` (client and server; verified
  via `/api/version`). The axis-1 v1 campaign ran on `0.24` — note this as a
  changed variable for any strict rerun/comparison. The `fix/prewarm-num-ctx`
  fix was **revalidated on 0.31.1** (2026-07-03): prewarm at `num_ctx=8192`
  still pins `/api/ps` CONTEXT to `8192`, so prewarm semantics did not change
  across the `0.24 → 0.31.1` jump.

## Local models (as of this writing)

12 models present; the 6 marked ✅ were tested in the axis-1 tool-call-fidelity
campaign (see `docs/runs/axis-1-findings.md`).

| ✓ | Model | Size |
|:---:|---|---:|
| ✅ | `qwen2.5-coder:7b` | 4.7 GB |
| ✅ | `qwen2.5-coder:14b` | 9.0 GB |
| ✅ | `qwen2.5:7b-instruct` | 4.7 GB |
| ✅ | `gemma4:e4b` | 9.6 GB |
| ✅ | `mistral-nemo:12b-instruct-2407-q4_K_M` | 7.5 GB |
| ✅ | `qwen3:30b` | 18.6 GB |
| — | `qwen2.5-coder:32b` | 19.9 GB |
| — | `gemma4:26b` | 18.0 GB |
| — | `qwen2.5:7b-instruct-q4_K_M` | 4.7 GB |
| — | `classification-12b:latest` | 7.5 GB |
| — | `classification-7b:latest` | 4.7 GB |
| — | `nomic-embed-text:latest` | 0.3 GB |
