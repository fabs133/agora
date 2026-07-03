# Prompting strategies — axis-1 v2, Phase 2

*Pre-registered 2026-07-03, before any v2 run executes. Grounded in
`docs/research/model-roster.md` (Phase 1). Every strategy carries the
five required fields: mismatch, target strategy, inheritance, expected
measurement, falsification criterion. Primary metrics are chosen to
match each strategy's mechanism — a text-channel strategy is judged on
pass_rate, not structured_emission_rate (see the plan's decision log).*

*Comparison baseline for all criteria: the model's own v2 CONTROL cells
(same daemon). v1 cells are secondary reference only.*

*Cell arithmetic: 3 treatment runs × 3 tasks = 9 task-cells per
strategized model. `loop_depth` was 0/36 in v1 and is expected to stay
at 0; thresholds below are therefore effectively over the 6
small_chain/content_robustness cells, but are stated over all 9 so a
loop_depth pass counts (it would be the strongest possible signal).*

---

## Strategy: `qwen2_5_coder`  (applies: qwen2.5-coder:7b, :14b)

**Mismatch.** Model not trained on tool-call tokens (roster, [D]).
Under the default protocol it narrates intent instead of calling; v1
measured ~0.67 calls/task, all via text-fallback extraction, 0/18 pass.
The failure is loop breakdown (insufficient, non-sustained emission),
NOT parsing — Agora's text-fallback already extracts the ```json blocks
it does emit.

**Target strategy.** `wrap_system`: append a few-shot block to the
system prompt containing two complete worked examples of tool-call
turns in the manifest's exact format (one `read_file`, one
`write_file`, realistic arguments), followed by two explicit rules:
emit exactly one tool call per turn; never describe an action in prose
without emitting the corresponding call. `wrap_tools` and
`wrap_messages` are identity.

**Inherits from.** vLLM's qwen2.5-coder parser + few-shot template
approach (vllm#32926), which reached 100% pass on 7B/32B in their
harness. Agora analog: the text-fallback parser is already the
"coder parser"; the few-shot injection is the missing half.

**Expected measurement.** Primary: `pass_rate` and `tool_calls_total`
per task. Expectation: calls/task rises from ~0.67 toward chain length
(3 for small_chain), pass_rate rises from 0. Secondary (recorded, not
judged): `structured_emission_rate` — MAY stay at 0 if the model keeps
emitting via the text channel; that is not failure. `text_fallback_rate`
near 1.0 with high call volume is a success shape for this model.

**Falsification criterion.** The strategy is FALSIFIED if, over the 9
treatment task-cells: pass_rate = 0/9 AND mean tool_calls_total per
task < 2× the v2-control mean. SUCCESS: pass_rate ≥ 3/9. PARTIAL
(strategy kept as special case, no resolver evidence): 1–2/9 passes,
or 0 passes with ≥2× call volume (emission fixed, completion still
broken — a different, narrower problem).

**Known confound (recorded 2026-07-03, Phase 3 review).** The rendered
system section for this model contains contradictory instructions: our
few-shot block requests fenced JSON-code-block emission, while the
model's own Ollama template appends a Tools block stating "Do not
include any backticks or ```json" *after* it. Assessment at
pre-registration review: acceptable, because (a) v1 shows the model
disregards that admonition anyway (all 12 v1 extractions were fenced
blocks emitted despite it), and (b) the strategy deliberately aligns
with the model's natural emission channel. Consequence for Phase 5: if
the strategy is FALSIFIED, "instruction contradiction suppressed
emission" is an alternative explanation the findings must address
before concluding capability floor — the designated disambiguation is a
single follow-up arm with `wrap_tools` returning an empty manifest
(tools described in the system prompt only, removing the contradicting
template block).

**Size invariance check.** 7b and 14b were byte-identical in v1. If the
strategy moves 7b but not 14b (or vice versa), that breaks the
training-regime explanation and is itself a finding — record it.

---

## Strategy: `mistral_nemo`  (applies: mistral-nemo:12b-instruct-2407-q4_K_M)

**Mismatch.** Two-layer. (1) Format: Mistral control-token convention
([AVAILABLE_TOOLS] / [TOOL_CALLS] / [TOOL_RESULTS]), not Hermes
(roster, [D]). (2) Mechanism, verified 2026-07-03 to near-certainty:
the Ollama template injects the tool manifest only when a user message
is within the last 2 messages, and the system prompt only when the
user message IS the last message. Agora's adapter emits tool results
as role "tool" (`llm_adapter.py::OllamaAdapter.format_tool_results`),
so mid-loop history is [user, assistant, tool, ...] with the sole user
message at index 0. Consequence: **from the second generation onward
the model receives neither the system prompt nor the tool manifest.**
Mechanism **[verified 2026-07-03, daemon 0.31.1]**: `ollama show
--template` gives the exact go_template — `[AVAILABLE_TOOLS]` renders
before a user message only when `(le (len (slice $.Messages $i)) 2)`
(user within the last 2 messages), and `$.System` renders inside
`[INST]` only when `(eq (len (slice $.Messages $i)) 1)` (user is the
last message). The daemon confirms it executes this template
(`template selection ... selected=go_template ... "[completion tools]"`).
A/B against the live daemon with the synthetic history [user,
assistant(tool_calls), tool]: the mid-loop case (trailing `tool`)
renders `prompt_len=198` — byte-exact to the reconstructed
`[INST]…[/INST][TOOL_CALLS]…[TOOL_RESULTS]…` with **no
`[AVAILABLE_TOOLS]` and no system** (system would be 255, the tool
block +559); appending one trailing user turn jumps the render to
`prompt_len=763` (tools + system reappear). So mid-loop the manifest
and system are confirmed absent. Gate below is VERIFIED — premise holds,
proceed to Phase 4.

**Target strategy.** `wrap_messages`: if the trailing message has role
"tool", convert it to role "user" with content prefixed
"Tool results:\n" (payload preserved verbatim, including the
[name#id] markers). This makes both template injection conditions fire
on every generation — system prompt and manifest are re-established
each turn. `wrap_system` and `wrap_tools` are identity.

Tradeoff, considered and accepted: converted results lose their
[TOOL_RESULTS] control-token framing, deviating from the trained
convention. The alternative — a derived Modelfile with a corrected
template — stays in-format but creates a new model tag (new roster
row, new provenance surface) and is more infrastructure than the
experiment needs. Default: wrapper first. If the wrapper result is
ambiguous (PARTIAL below), the Modelfile variant is the designated
follow-up, not a bigger wrapper.

**Inherits from.** The template's own semantics (Mistral convention:
tools serialized before the last user message — docs.mistral.ai
tokenization cookbook) plus the common framework workaround of
user-role folding for Mistral-family agent loops.

**Expected measurement.** Primary: `tool_calls_total` per task
(sustained emission across turns) and `pass_rate`. Expectation: if the
manifest-drop mechanism is the binding constraint, emission stops
collapsing after turn 1 and calls/task rises sharply; pass_rate moves
off 0. Secondary (recorded): the structured/text emission split —
v1's ~50/50 mix may shift either way; no prediction registered.

**Falsification criterion.** FALSIFIED if pass_rate = 0/9 AND mean
tool_calls_total per task < 2× v2-control — i.e. re-establishing
system+manifest every turn changes nothing; the v1 behaviour is then a
capability floor, not a template artifact. SUCCESS: pass_rate ≥ 2/9
(threshold lower than coder's: format deviation of the folded results
is a known headwind). PARTIAL: 1/9, or 0 passes with ≥2× call volume →
run the Modelfile variant as v2.1 before concluding.

**Pre-campaign gate.** If the OLLAMA_DEBUG dump shows the manifest is
NOT dropped mid-loop (mechanism refuted), this strategy's premise is
dead: do not run the treatment arm as designed; return to Phase 2 and
redesign around format compensation only. The gate exists so the
campaign never tests a strategy whose stated mechanism is already
known false. — **RESULT [2026-07-03]: PASSED.** Manifest and system are
dropped mid-loop on daemon 0.31.1 (evidence in Mismatch above:
`prompt_len` 198 trailing-`tool` vs 763 trailing-`user`). Premise
holds; the `mistral_nemo` strategy proceeds as designed.

---

## Default-strategy declarations (no change expected)

Required by the plan: every model without a documented mismatch is
explicitly marked.

- **qwen2.5:7b-instruct — default.** Format is native Hermes; v1
  fragility (turn-1 bailing) is behavioural, not format. A
  fragility-targeting strategy (first-turn tool-use reinforcement) was
  considered and DEFERRED: the model's value in v2 is as a drift
  sentinel, and adding a treatment would spend that. Candidate for v3.
- **gemma4:e4b — default.** Strongest tool-caller in the set; no
  mismatch. Drift sentinel. Note from roster: its rendering/parsing is
  daemon-internal, so it is the most daemon-version-coupled model —
  sentinel divergence is interpreted as daemon delta, not regression.
- **qwen3:30b — default.** Format is native Hermes. The open question
  (findings OQ3: does prompting affect the bistability?) is DEFERRED:
  v2's 5+5 null/null cells establish the intrinsic bistability baseline
  post-prewarm-fix first. Testing a prompting variation before having
  that baseline would conflate two unknowns.

## What a v2 outcome table looks like (pre-committed shapes)

| Outcome | coder | nemo | Reading |
|---|---|---|---|
| Both SUCCESS | ≥3/9 | ≥2/9 | Strong resolver evidence (plan Phase 5, option 1) |
| One SUCCESS | — | — | Weak evidence: keep winner as special case, defer resolver |
| Both FALSIFIED | 0/9, <2× calls | 0/9, <2× calls | v1 behaviours are capability floors; no resolver; axis-1 closed |
| Any PARTIAL | 1–2/9 or calls-only | 1/9 or calls-only | Named follow-up (coder: none — accept; nemo: Modelfile v2.1) |

## References

- docs/research/model-roster.md (Phase 1; all external citations live there)
- src/agora/fleet/llm_adapter.py — format_tool_results (role="tool"
  emission, verified against the mistral template conditions)
- campaigns/axis-1-tool-call-fidelity-v2.yaml — treatment/control cell
  layout these criteria bind to
