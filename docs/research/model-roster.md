# Model roster — tool-call capability inventory (axis-1 v2, Phase 1)

*Compiled 2026-07-03. Method: local extraction from the Ollama manifest
store (`D:\ollama\models\manifests`, blob templates read directly — no
daemon required) plus web research. Every row answers: does this model
have native tool-call training, and if so in which format? Confidence is
marked per claim: **[D]** well-documented, **[P]** partially-documented,
**[S]** speculation/inference — [S] claims need verification before a
Phase 2 strategy is built on them.*

*Local template evidence: blob digests recorded per row. Ollama layer
mediaTypes inspected: model / system / template / params / license.*

---

## Swept models (axis-1 v1 set)

### qwen2.5-coder:7b and qwen2.5-coder:14b

| Field | Value |
|---|---|
| Ollama tags | `qwen2.5-coder:7b`, `qwen2.5-coder:14b` |
| Base | Qwen2.5-Coder-7B/14B-Instruct (coder-specific instruct variants) |
| Family | qwen2.5-coder |
| Advertised tool support | Ollama exposes tools; **not trained on tool-call tokens** [D] |
| Native tool-token format | **None.** Tool-call output is template-induced only [D] |
| Behavioural class (v1) | narrate-fallback, 0/18 pass, byte-identical across 7b/14b |

**Training regime [D]:** Qwen2.5-Coder was not trained with Hermes-style
tool tokens, unlike Qwen2.5-Instruct. Documented by the vLLM community
(vllm issue #32926, coder-specific parser proposal) and independently in
Ollama context (hermes-agent issue #5867: calls returned as JSON strings
in content, not the tool_calls array). Both already cited in
`docs/runs/axis-1-findings.md` References.

**Ollama template (local) [D]:** one shared blob across **all three**
coder sizes (7b, 14b, 32b):
`sha256:1e65450c...d320` (1615 B). Hermes-shape induction with an
explicit admonition appended: *"with NO other text. Do not include any
backticks or ```json."* — the template authors are visibly compensating
for the exact code-block emission failure v1 measured. The template also
carries FIM tokens (`<|fim_prefix|>` etc.), coder-specific.

**Strategy implication:** because the template blob is shared, any
`qwen2_5_coder` strategy validated on 7b/14b transfers unchanged to 32b
if it ever enters a sweep. vLLM's coder parser + few-shot template
reached 100% pass on 7B/32B (issue #32926) — the strongest external
prior for Phase 2 hypothesis 1.

### qwen2.5:7b-instruct

| Field | Value |
|---|---|
| Ollama tag | `qwen2.5:7b-instruct` |
| Base | Qwen2.5-7B-Instruct |
| Family | qwen2.5 |
| Advertised tool support | Yes, native [D] |
| Native tool-token format | **Hermes-style `<tool_call>` JSON** [D] |
| Behavioural class (v1) | structured-fragile (turn-1 bailing) |

**Training regime [D]:** Qwen2.5-Instruct trained with Hermes-style tool
markers; the family default per Qwen's official function-calling docs
(qwen.readthedocs.io/en/latest/framework/function_call.html); vLLM docs
confirm the hermes parser applies out of the box for Qwen2.5.

**Ollama template (local) [D]:** `sha256:eb440283...5175` (1482 B),
standard Hermes shape, no "NO other text" admonition — consistent with a
trained format that needs no compensation.

**v1's fragility is not format-related [P]:** zero text-fallback across
18 tasks; when it emits, it emits structured. The turn-1 bailing is a
behavioural quirk, not a format mismatch. If Phase 2 assigns a strategy
here it targets fragility (e.g. explicit "you must call a tool on your
first turn" system-prompt reinforcement), not format.

### quant variant: qwen2.5:7b-instruct-q4_K_M — **STRUCK**

Verified locally 2026-07-03: all four layers (model, system, template,
license) have **identical digests** to `qwen2.5:7b-instruct`
(model blob `sha256:2bada8a7...3730`, 4.68 GB). The default tag already
IS q4_K_M; there is no quant comparison to run. Row closed [D].

### gemma4:e4b

| Field | Value |
|---|---|
| Ollama tag | `gemma4:e4b` |
| Base | Gemma 4 E4B instruction-tuned (E = "effective" params, edge variant) |
| Family | gemma4 (Google DeepMind) |
| Advertised tool support | Yes — "native function-calling support" [D] |
| Native tool-token format | Gemma 4 native format, **rendered in-daemon by Ollama** [P] |
| Behavioural class (v1) | structured-succeeds (best in set) |

**Training regime [D]:** Gemma 4 advertises native function calling and
native system-role support (Ollama model card ollama.com/library/gemma4;
Google function-calling docs
ai.google.dev/gemma/docs/capabilities/text/function-calling-gemma4).

**Ollama handling (local + web) [D/P]:** the manifest carries **no
template layer** — only model, license, and a params blob
(`{"temperature":1,"top_k":64,"top_p":0.95}`; the campaign's
temperature=0 override supersedes these). Ollama renders gemma4's chat
format natively in the daemon ("Ollama already handles the complexities
of the chat template for you" — model card). The gemma4 tool-call path
has its own fix history inside Ollama itself: parsing fixed in 0.20.2
(ollama/ollama#15241), tool-calling rework in PR #15306 (both referenced
from anomalyco/opencode#20995).

**Drift-sentinel consequence [P]:** gemma4's prompt rendering and
tool-call parsing live in the daemon binary, not in a pinned blob. Of
all six models it is the one whose behaviour is MOST coupled to the
Ollama version — which makes it the most sensitive drift detector, and
also means a v1↔v2 divergence in its cells is expected rather than
alarming. Interpret its sentinel data with that in mind.

### mistral-nemo:12b-instruct-2407-q4_K_M

| Field | Value |
|---|---|
| Ollama tag | `mistral-nemo:12b-instruct-2407-q4_K_M` (exact local tag; short tag 404s) |
| Base | Mistral-NeMo-Instruct-2407 (Mistral AI + NVIDIA) |
| Family | mistral (Tekken tokenizer generation) |
| Advertised tool support | Yes — "trained on function calling" [D] |
| Native tool-token format | **Mistral control-token format**, NOT Hermes [D] |
| Behavioural class (v1) | mixed-fails (structured/text split ~50/50, 0/18 pass) |

**Training regime [D]:** officially trained on function calling
(mistral.ai/news/mistral-nemo). The format is fundamentally different
from every other model in the set: `[AVAILABLE_TOOLS]…[/AVAILABLE_TOOLS]`
for the manifest, `[TOOL_CALLS]` for emission, `[TOOL_RESULTS]` for
results — and these are **control tokens in the Tekken tokenizer**, not
literal text (Mistral tokenization cookbook,
docs.mistral.ai/cookbooks/concept-deep-dive-tokenization-tool_calling).

**Ollama template (local) [D]:** `sha256:438402dd...075a` (683 B).
Faithful to the Mistral shape, with two observed properties that matter:

1. **Mid-loop manifest drop [S — verify before building on it]:** the
   template injects `[AVAILABLE_TOOLS]` only when a *user* message sits
   within the last 2 messages
   (`if and $.Tools (le (len (slice $.Messages $i)) 2)`). Agora's agent
   loop appends tool results with role `tool`, so from the second
   generation onward the last user message is deep in the history and
   the condition never fires — **later turns are rendered with no tool
   manifest at all.** This is a concrete candidate mechanism for v1's
   mixed-fails class. Verification path: dump the rendered prompt for a
   3-turn synthetic history (Ollama debug or template unit-render) and
   confirm the manifest is absent.
2. **No tool-call IDs [P]:** the template omits call IDs entirely.
   Mistral's own convention expects 9-character tool-call IDs in
   `[TOOL_RESULTS]` pairing (noted in vLLM's tool-calling docs for
   Mistral models). Whether Nemo degrades without IDs is undocumented;
   lower-priority than (1).

**Community quirks [D]:** even under vLLM with the dedicated
`--tool-call-parser mistral` and Mistral's own chat template, Nemo tool
calls land in `content` instead of `tool_calls` (vllm issue #33684) —
independent corroboration that the mixed-emission behaviour is
model-side, not purely an Ollama artifact.

**Strategy implication:** Phase 2's `mistral_nemo` strategy has two
separable hypotheses now: (a) re-inject the manifest every turn (fixes
the [S] mechanism if real), (b) format compensation for content-channel
emission. (a) is testable first and cheaper.

### qwen3:30b

| Field | Value |
|---|---|
| Ollama tag | `qwen3:30b` |
| Base | Qwen3-30B-A3B (MoE, ~3B active) |
| Family | qwen3 |
| Advertised tool support | Yes, native [D] |
| Native tool-token format | **Hermes-style `<tool_call>` JSON** (same as qwen2.5-instruct) [D] |
| Behavioural class (v1) | structured-fragile + steady-state non-determinism (0.67) |

**Training regime [D]:** Hermes-style tool use is in the Qwen3 chat
template per official docs (qwen.readthedocs.io function_call guide;
vLLM recommends the hermes parser for Qwen3; Red Hat AI Inference docs
concur). Qwen's docs explicitly warn against stopword-based tool
templates (e.g. ReAct) for reasoning models because stopwords can appear
inside the thought section — relevant context for the `<think>`-stripping
Agora already does.

**Ollama template (local) [D]:** `sha256:2d54db2b...97f9` (1506 B).
Hermes shape plus thinking-block handling (`IsThinkSet`, `<think>`
sections, generation primed with an open `<think>` tag). Tool results
are wrapped as `<tool_response>` inside a user-role turn — same
convention as qwen2.5-instruct.

**Agora-internal quirks (v1 data, not external):** systematic
`mark_complete` schema confusion (invokes it with a file-writing tool's
arguments, all 9 malformed calls identical) and bistable steady-state
trajectories under nominally deterministic settings (MoE routing
hypothesis). v2 allocates 5+5 repeats to characterize the latter with
the prewarm fix in place.

---

## Candidates (not in v2)

### qwen2.5-coder:32b

Same shared template blob as 7b/14b [D] — a validated coder strategy
transfers unchanged. Excluded from sweeps by the VRAM gate
(num_ctx=4096 required on the P40, a changed variable vs the sweep's
8192). Row status: researched, deliberately excluded.

### gemma4:26b

**MoE variant — "26B A4B"** per Google's Ollama integration docs
(ai.google.dev/gemma/docs/integrations/ollama) [D]. Same no-template-
layer handling as e4b (verified locally: model + license + params only).
Untested. If it ever enters a sweep, note it pairs with qwen3:30b on the
MoE-determinism question. Row status: researched, not scheduled.

## Out of scope

- `classification-12b` / `classification-7b`: task-specific local
  fine-tunes, not tool-call candidates. Not researched further.
- `nomic-embed-text`: embedding model. Not applicable.

---

## Format landscape summary

| Format | Models | Manifest delivery | Emission marker |
|---|---|---|---|
| Hermes-style JSON | qwen2.5:7b-instruct, qwen3:30b | system-prompt `<tools>` block | `<tool_call>` tags |
| Hermes-induced (untrained) | qwen2.5-coder 7b/14b/(32b) | system-prompt `<tools>` block | `<tool_call>` requested, ```json emitted |
| Mistral control tokens | mistral-nemo:12b | `[AVAILABLE_TOOLS]` before last user msg | `[TOOL_CALLS]` token |
| Gemma 4 native (daemon-rendered) | gemma4:e4b/(26b) | in-daemon renderer, no blob | daemon-parsed |

Three genuinely distinct conventions in a six-model set — the
per-model-strategy hypothesis space Phase 2 works from.

## Phase 1 checkpoint status

Every swept model answers the checkpoint question with a citation.
One [S] claim is flagged as a Phase 2 gate: the mistral-nemo mid-loop
manifest drop must be verified by rendered-prompt inspection before the
`mistral_nemo` strategy is designed around it.

## References

- Qwen2.5/Qwen3 function calling (Hermes format, training-regime):
  https://qwen.readthedocs.io/en/latest/framework/function_call.html
- vLLM #32926 — Qwen2.5-Coder not trained on tool tokens; coder parser:
  https://github.com/vllm-project/vllm/issues/32926
- hermes-agent #5867 — coder JSON-in-content in Ollama context:
  https://github.com/NousResearch/hermes-agent/issues/5867
- Mistral NeMo announcement (trained on function calling):
  https://mistral.ai/news/mistral-nemo/
- Mistral tokenization cookbook ([TOOL_CALLS] et al. as control tokens):
  https://docs.mistral.ai/cookbooks/concept-deep-dive-tokenization-tool_calling
- vLLM #33684 — Nemo tool calls in content despite mistral parser:
  https://github.com/vllm-project/vllm/issues/33684
- vLLM tool-calling docs (Mistral 9-digit tool-call IDs; Qwen hermes):
  https://docs.vllm.ai/en/stable/features/tool_calling/
- Gemma 4 Ollama model card (native function calling, daemon-side
  template): https://ollama.com/library/gemma4
- Google — function calling with Gemma 4:
  https://ai.google.dev/gemma/docs/capabilities/text/function-calling-gemma4
- Google — Gemma with Ollama (26B = A4B MoE):
  https://ai.google.dev/gemma/docs/integrations/ollama
- opencode #20995 — gemma4 tool-call fix history in Ollama (0.20.2 fix,
  PR #15306 rework): https://github.com/anomalyco/opencode/issues/20995
- Local extraction 2026-07-03: manifest layer digests + template blobs,
  `D:\ollama\models` (daemon not required; read directly from disk).
