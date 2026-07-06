# Ollama operations — this device

Operational reference for driving Ollama on this machine: **Windows 11,
dual-GPU** (GPU 0 = Tesla P40 24 GB, GPU 1 = RTX 3060 Ti 8 GB). This is the
box the axis-1 characterization campaign ran on.

## 1. Start the daemon

Ollama is **not** running by default here. Start a **bare `serve`** with the
environment set inline — do not rely on the tray app / scheduled task (see
[Gotchas](#6-gotchas-on-this-box)):

```bash
OLLAMA_MODELS='D:\ollama\models' OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 ollama serve
```

## 2. Required environment

| Var | Value | Why |
|---|---|---|
| `OLLAMA_MODELS` | `D:\ollama\models` | The models live here. The default (`<home>\.ollama\models`) is **empty** — omit this and every model looks "missing." |
| `OLLAMA_NUM_PARALLEL` | `1` | `>1` multiplies KV-cache VRAM and caused intermittent `GGML_ASSERT(mem_buffer != NULL)` crashes (~11% of tasks) on the P40. |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | One model at a time — 24 GB can't hold two of the larger models. |

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
eviction protocol. Commit `55de631` (`fix/prewarm-num-ctx`, merged to `main`) is
what makes prewarm carry `num_ctx` — without it, each model block's first run
loads at 32768 and is not comparable to its steady-state repeats. Confirmed live
on daemon `0.31.1`: the harness logs `evicted → pre-warmed gemma4:e4b` and
`/api/ps` then reads `context_length=8192`.

## 6. Gotchas on this box

- **Tray app / `Ollama-Server` scheduled task respawns `ollama` on :11434 with
  the wrong config** (default models dir, multi-load), which blocks a manual
  `serve` from binding. That scheduled task is currently left **disabled**. If a
  `serve` fails to bind with "address in use," a stray tray instance is already
  up — kill it first, or use the running instance only if its `/api/tags` shows
  the full 12 models.
- **A `serve` started from a background shell dies when that shell/session is
  torn down.** Re-run the start command when you come back.
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
