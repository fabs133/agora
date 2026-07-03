# Axis-1 v2 plan — per-model prompting strategies

*Status: active. Owner split: chat-Claude (research, design, spec, review) /
Claude Code (implementation, execution, mechanical docs). This document is
the contract between the two; neither side deviates from it silently.*

Campaign artifact: `campaigns/axis-1-tool-call-fidelity-v2.yaml` (draft;
fails `load_campaign()` until Phase 3 lands — intentional).

## Standing policy: track latest Ollama

The daemon follows the latest release (currently 0.31.1; v1 ran on 0.24).
Consequences, permanent:

- Every campaign carries **same-daemon controls**: the strategy A/B runs
  within one campaign, so the primary comparison never crosses daemon
  versions. v1↔v2 comparisons are secondary and restricted to control cells.
- **Drift sentinels**: at least one fully-default-config model per campaign.
  gemma4:e4b is the anchor — byte-deterministic on 0.24, so any v1↔v2
  divergence in its cells is the measured daemon delta.
- `run.jsonl` already records `ollama_version` per run (jsonl.py
  `query_ollama_version`); no work needed there.
- `OLLAMA.md` and any doc stating a daemon version gets updated whenever the
  daemon moves (Claude Code, mechanical).

## Phase 0 — environment revalidation  [Claude Code]

1. Merge `fix/prewarm-num-ctx` (55de631) to `main`. It is currently one
   commit ahead of main, unmerged.
2. Revalidate the prewarm fix against daemon 0.31.1: start serve per
   OLLAMA.md, prewarm gemma4:e4b with `options.num_ctx=8192`, confirm
   `/api/ps` CONTEXT reads 8192, evict. Newer daemons may have changed
   prewarm semantics; the fix was written against 0.24-era behaviour.
3. One smoke run: gemma-e4b profile, tool-call-fidelity probe, single run
   via the campaign harness path. Confirms the full stack works on 0.31.1
   before anything else is built on top.
4. Update `OLLAMA.md` version references (0.24 → current).

**Acceptance:** fix merged; `/api/ps` shows pinned context after prewarm on
the current daemon; one clean run.jsonl produced; docs updated.

## Phase 1 — roster and capability inventory  [chat-Claude]

Deliverable: `docs/research/model-roster.md`. As specified in the original
plan (tag, base+variant, family, advertised tool support with citation,
training-regime notes, Modelfile template shape via `ollama show
--modelfile`, community quirks, per-row confidence), plus two additions:

- **Native tool-token format column**: Hermes-style `<tool_call>` vs
  Mistral's `[TOOL_CALLS]` control-token format vs none. This column is what
  the mistral_nemo strategy hangs on (findings Open Question 2).
- **Blob-digest check** for qwen2.5:7b-instruct-q4_K_M vs
  qwen2.5:7b-instruct (`ollama show` digests). If identical, the quant
  candidate is struck from the roster with that note — the default tag is
  often already q4_K_M.

Scope: the six swept models. qwen2.5-coder:32b and gemma4:26b get rows
marked "candidate, not in v2" (32b breaks param comparability via its
num_ctx=4096 VRAM gate).

**Acceptance:** unchanged from the original plan.

## Phase 2 — per-model strategy design  [chat-Claude]

Deliverable: `docs/research/prompting-strategies.md`. The original five
fields per strategy (mismatch, target strategy, inheritance, expected
measurement, falsification criterion), with one amendment:

**The primary metric must match the strategy's mechanism, pre-registered:**

- A strategy inducing *native* structured emission is judged on
  `structured_emission_rate`.
- A strategy legitimizing the *text channel* (vLLM-style few-shot with
  ```json blocks, which Agora's text-fallback parser already extracts) is
  judged on `pass_rate` and `tool_calls_total` per task —
  `structured_emission_rate` is expected to stay at 0 by design and must not
  be read as failure.

Rationale: v1 shows coder's failure was not parsing (12 text-fallback
extractions succeeded) but loop breakdown (~0.67 calls/task, then
narration). Judging a text-channel strategy on structured emission would
falsify a succeeding strategy.

Strategy names in the campaign YAML (`qwen2_5_coder`, `mistral_nemo`) are
placeholders; Phase 2 may revise assignments (including adding one for
qwen-instruct's turn-1 bailing or qwen3), but the YAML changes only after
this document is updated. Pre-registration before execution.

## Phase 3 — minimal strategy mechanism  [Claude Code, spec below]

Budget: total diff under ~200 lines including tests. If it grows past that,
stop and flag — we are over-building.

### Shape

    src/agora/fleet/strategies/
      __init__.py        # STRATEGIES registry + resolve(name) -> Strategy | None
      qwen2_5_coder.py   # per Phase 2 design
      mistral_nemo.py    # per Phase 2 design

- `Strategy` protocol: `wrap_system(prompt: str) -> str`,
  `wrap_tools(tools: list) -> list`, `wrap_messages(messages: list) -> list`.
  Default (base) implementations are identity.
- `StrategyAdapter(inner: LLMProtocol, strategy)` — wrapper, not a
  modification of OllamaAdapter. Applies the three wraps around
  `complete(messages, system, tools)`; passes `format_assistant_turn` /
  `format_tool_results` through untouched.
- **When `strategy is None`, no wrapper is constructed at all.** The
  byte-identical acceptance criterion then holds structurally, not by proof.

### Plumbing (mirrors the existing AGORA_ARM_* pattern exactly — runs are
### child processes, so the name travels via env)

1. `CampaignRun` gains `strategy: str | None = None` (`extra='forbid'`
   makes this a required schema addition).
2. `load_campaign()` validates every non-null strategy name against the
   registry — unknown names fail at load, not at run 23 of 40. Consistent
   with the repo's "typos are rejected loudly" convention.
3. `build_env()` emits `AGORA_STRATEGY=<name>` when set.
4. `run_tool_call_fidelity.py` reads the env var, resolves via the registry,
   wraps the adapter.
5. Provenance: `RunRecord` gains `strategy: str | None = None`. Additive
   optional field; `schema_version` stays 1 — old files parse unchanged,
   locked invariants untouched.
6. `layer2` / `capability_vectors.csv`: add `campaign` and `strategy`
   columns so v1 and v2 rows coexist; cells key on
   `(model, strategy, sub_target)` for v2.

### Tests (minimum)

- `strategy=None` ⇒ `build_env` contains no `AGORA_STRATEGY` and the
  adapter object is the bare inner adapter (identity by construction).
- Unknown strategy name in a campaign YAML ⇒ `load_campaign` raises.
- Registry strategy applied ⇒ `complete` receives wrapped system/tools
  (assert on a stub inner adapter).

**Acceptance:** unchanged from the original plan, with "byte-identical to
axis-1 v1" scoped to same-daemon behaviour (the runner code path, not the
model output — the daemon moved, and that delta is what the sentinels
measure).

## Phase 4 — controlled re-campaign  [Claude Code executes, chat-Claude
## reviews checkpoints]

`campaigns/axis-1-tool-call-fidelity-v2.yaml`, 40 runs, staged via
`scripts/run_sweep_staged.py` with the same pause discipline as v1
(after r001, after each model block, before each new model).

Design already encoded in the YAML header; key points:

- A/B on `strategy`, arm untouched (lean/strict uniformly).
- Control runs precede treatment runs within each model block.
- Comparisons: within-v2 control-vs-treatment is primary (same daemon);
  v1↔v2 on control cells measures daemon drift; steady-state-to-steady-state
  only (v2 block-firsts are clean post-fix; v1 block-firsts were not).
- Probe unchanged, including `loop_depth` (0/36 in v1). Kept for
  comparability; pre-registered exception: if a coder strategy works,
  loop_depth becomes the first task that could reveal a strategy ceiling.
  Graduated loop-depth is deferred to a separate probe.

**Acceptance:** all 40 runs complete; JSONL clean; Layer 1 pipeline runs;
comparison table exists (per model × metric: v1, v2-control, v2-treatment).

## Phase 5 — findings and resolver decision  [chat-Claude drafts,
## both review]

As in the original plan (three-way decision gate: strong / weak / no
evidence for a resolver layer), with the comparison table extended to three
columns per cell (v1, v2-control, v2-treatment) so daemon drift and strategy
effect are separately attributable.

## Decision log (delta over the original plan)

- **Track-latest daemon policy** accepted; drift controlled by same-daemon
  A/B + permanent drift sentinels, not by version pinning.
- **A/B within v2** rather than pure v1↔v2 comparison — isolates strategy
  effect from the 0.24→0.31.1 daemon delta at zero extra run cost.
- **`strategy` is a first-class CampaignRun field**, not an arm overload —
  arm keeps its original reserved semantics.
- **Per-strategy primary metric pre-registered by mechanism** — text-channel
  strategies are judged on pass_rate, not structured_emission_rate.
- **Wrapper adapter, identity-by-construction for None** — no OllamaAdapter
  changes; the null path constructs nothing.
- **RunRecord: additive optional `strategy` field, schema_version stays 1.**
- **qwen3 gets 5+5** — 3 repeats cannot distinguish a strategy effect from
  bistable mode sampling at 0.67 reproducibility.
- **v2 scope stays at the six tested models** — coder-32b's num_ctx VRAM
  gate is a changed variable, not a seventh row; new candidates go in a
  separate mini-campaign.
