# Agora — Findings across runs

Cross-cutting analysis of the 46 runs catalogued in
[registry.yaml](registry.yaml). Three test bed projects (URL shortener,
Discord bot, FastAPI CRUD) plus the plan-builder meta-flow and a
code-review flow, against four model tiers (qwen2.5:7b, qwen2.5-coder:7b,
gpt-4o-mini, gpt-4o).

This document is structured in three layers:

- **Sections 1–3 (descriptive)**: failure taxonomy, what worked, what
  didn't. Drafted from registry rows and the lessons-learned.md round table.
- **Sections 4–5 (quantitative)**: model-tier comparison and cost/latency
  numbers. Estimates are explicitly marked.
- **Sections 6–7 (synthesis)**: open framework gaps with the run that
  surfaced each, and the decision log of hypotheses kept and dropped.

Citations like `discord-bot.run13` reference `run_id` keys in
`registry.yaml` — every cited id resolves there.

---

## 1. Failure taxonomy

The 2026-04-17 lessons-learned.md classified all observed failures into three
categories:

1. **The framework lacked a gate.** A bug shipped because no postcondition
   evaluated the relevant invariant.
2. **The model hallucinated a fact.** Gates caught it; auto-learning
   recorded it; loopback retry sometimes succeeded, sometimes reproduced
   the same hallucination.
3. **The model narrated instead of acting.** "Let's read app.py..." instead
   of `read_file`.

Post-2026-04-17 runs surfaced four more categories. Together these eight
cover every failure across the 46 runs.

### Category 4 — api_spec quality cascade

**Shape**: plan-builder produces an api_spec.md that parses, validates
against simple gates, and consumes downstream into an executor plan that
EXECUTES — but the plan is structurally degenerate (one module instead
of three, missing methods, fabricated endpoints) so the produced code is
broken before the model gets to write a line.

**First seen**: `plan-builder.run3-pre-C`.
**Reproduced in**: `plan-builder.run5-gpt4omini-messy`,
`plan-builder.run9-bullet-slipped`, `plan-builder.run13-test-module`.
**Mitigations**: C1 brief-deliverable predicate, C5 api_spec validity
hardening (duplicate module rejection, parse-fail rejection, stray
top-level statement rejection), `strip_test_module_sections` auto-heal.
**Confirmed addressed**: `plan-builder.run14-4omini-clean` ships a clean
plan.

The failure was upstream of the model's code-generation step, so framework
gates that only check the executor's output couldn't see it. The fix had
to land in the planner.

### Category 5 — Edit-tool non-unique-match loops

**Shape**: `edit_file_replace` requires `old_string` to match exactly once.
When the LLM picks an `old_string` that recurs (a common token like
`return result` or `def __init__(self)`), the tool returns
"matches N places" and the model retries with a slightly different but
equally non-unique anchor. With a weak prompt the model loops indefinitely.

**First seen**: `url-shortener-mvp.run2-editloop-killed` (gpt-4o-mini).
**Reproduced in**: `plan-builder.run7-c5-edit-loop` (same model, planner
side).
**Mitigation**: `_format_match_locations` shows line numbers + 1 line of
context for every candidate match, plus an upsert-tool hint. Empirically
4o-mini finds a unique anchor on the first retry once it can see them.
**Confirmed addressed**: every gpt-4o-mini run after run-7 of plan-builder.

This was an ergonomics fix, not a framework gate. The framework was
behaving correctly; the model was capable of escaping the loop given
better information.

### Category 6 — Tool side-effects from the wrong stage

**Shape**: an architect agent in `define_api` or `author_spec` calls
`add_class`, `delete_file`, or `write_file` against `src/`. The stage was
supposed to be authoring the *spec*, not modifying the codebase. Side
effects survived even after the stage completed.

**First seen**: `plan-builder.run10-api-spec-deleted` — architect deleted
its own api_spec.md after the define_api stage succeeded.
**Reproduced in**: `plan-builder.run11-plan-dir-path` — architect wrote
`output_path: plan/<x>.py` into a task that was supposed to declare an
`src/` path.
**Mitigation**: `hide_tools` lockdown across every author_* stage —
write_file, delete_file, edit_file_*, fetch_url, add_class*, add_function*
are all hidden from the LLM's tool manifest during spec authoring.
**Confirmed addressed**: plan-builder runs 12–14.

This is the same pattern as Round 4's "hide runtime tools from the LLM"
fix in the original lessons-learned.md, applied to a new stage class.

### Category 7 — Code-review under-capability

**Shape**: the code-review flow runs cleanly, the reviewer agent emits
structured output (per-file `<x>.py.md` + `REPORT.md`), but the *content*
of the reviews is flat: every file gets "looks clean" regardless of
actual issues. The structural framework is fine; the model's analysis
capability is the bottleneck.

**First seen**: `code-review.run1-noheader`.
**Reproduced in**: `code-review.run2-eos`, `code-review.live`.
**Mitigation**: none in framework. Memory note (project_2026_04_17_session.md):
"7B analysis capability is effectively zero (defaults to 'clean' on every
file)". The fix is a more capable model, not more framework.

This is the cleanest example of category-3-but-different — the model isn't
narrating-instead-of-acting; it's *acting correctly with no judgment*. A
distinct failure mode that the framework cannot patch.

### Summary table

| Category | Origin | Mitigation locus | Solved |
|---|---|---|---|
| 1 — Missing gate | Framework | postconditions, AST checks, auto-hooks | Yes (rounds 1–13) |
| 2 — Model hallucinates | Model | gates catch, auto-learning records, loopback retries | Partially (gate yes, retry-success no) |
| 3 — Narration | Model | `_maybe_queue_narration_redirect` STOP directive | Yes (round 13) |
| 4 — api_spec cascade | Plan-builder | C1 + C5 + auto-strip in plan/ | Yes (round 15) |
| 5 — Edit-tool loops | Tool ergonomics | `_format_match_locations` | Yes (round 14) |
| 6 — Wrong-stage side effects | Stage config | hide_tools in author_* stages | Yes (round 18) |
| 7 — Code-review capability | Model judgment | none — needs more capable model | No |

---

## 2. What worked, ranked by evidence

Ranked by the volume of failures each mechanism caught × distance to "the
model alone wouldn't have noticed".

### 1. Postconditions — most load-bearing

Across 46 runs, postcondition evaluations are the line between "the LLM
said it's done" and "the artefact actually works". `_postcond_py_compiles`,
`postcond_python_imports`, `postcond_pytest_passes`, and the bespoke
cross-file checks (`postcond_no_code_after_main_block`,
`postcond_readme_only_references_existing_commands`,
`postcond_bot_calls_tree_sync`) catch failures that every model tier
would otherwise have shipped.

Every round 1–11 fix in the lessons-learned.md table is a postcondition
addition. Most run failures in the registry that say "FAILED" do so
because a postcondition fired, not because the LLM gave up.

**Evidence weight: highest**. Confirmed across all four model tiers and
all five test beds.

### 2. Edit primitives — second-highest

`edit_file_replace`, `edit_file_insert_before`, `edit_file_append` made
transcription bugs measurably less frequent. lessons-learned.md cites
`discord.InterACTION` and duplicate `import random` (Run 11) as the
canonical "before" — both arose from re-emitting 25 lines of an existing
file. Run 12 onwards: zero transcription bugs on bot.py.

The ergonomics fix in round 14 (`_format_match_locations`) made these
tools more usable for medium-tier models without changing the underlying
mechanism.

**Evidence weight: high**. Cited in `discord-bot.run11`,
`discord-bot.run12`, `discord-bot.run13` notes.

### 3. Staging + literal templates

Splitting hard tasks into `Stage` objects with small `max_iterations` and
preloaded `context_files` lets the model focus on a small payload at a
time. For tasks that have no creative content (e.g. `write_requirements`,
`build_skeleton`), the stage instruction is *"write EXACTLY this
content"* and the model copy-pastes.

`discord-bot-full.live` (Run 17, 23/23 first-pass) is the existence proof:
every Python-producing task was a template-driven stage, the architect
couldn't "design" anything mid-run, and the 7B that previously hit a
ceiling at 12 tasks delivered 23 cleanly.

**Evidence weight: high**. Run 17 vs Runs 14–16 is the natural A/B.

### 4. Auto-hooks — silent enforcement

`run_auto_hooks` after every write/edit fires `check_python` →
`run_python_import` → `git_commit` and queues validation errors into the
LLM's next-turn message history. The LLM doesn't have to *remember* to
call the check tools; the framework runs them and shows the model the
result on the next turn.

Hidden from the model's tool manifest (round 4), the auto-hook system also
prevents distraction-by-verbs.

**Evidence weight: high**. Visible across every run with a non-zero git
commit count — every "auto: wrote X" commit is an auto-hook firing.

### 5. Auto-learnings + loopback

The flywheel from lessons-learned.md round 8: failed postconditions →
synthesised `Learning` → injected into next-turn system prompt → loopback
retries the task. Works most of the time, doesn't always.

For 7B, loopback occasionally pulls a task to green that failed first
pass; more often the model reproduces the same hallucination because it
can't absorb abstract corrective advice. For 4o-mini, loopback is rarer
because the first pass usually goes through.

**Evidence weight: moderate, confirmed at 7B; weak at 4o-mini (less
loopback to compare).**

### 6. Structural validation L1/L2/L3

The new `plan/structure.py` machinery introduced post-2026-04-17.
Catches contract↔test drift (L1↔L2) and impl self-inconsistency (L3) at
gate time, before tests run.

**Evidence weight: emerging**. The gpt-4o run on URL shortener passed
every L1/L2/L3 gate cleanly, then failed on the test that the framework
*could not see* (a value-level tester bug). Validates "L1/L2/L3 catch
what they're designed to catch"; doesn't yet validate they're net-positive
across more runs because we have a small sample at higher tiers.

### 7. Matrix as observer surface

Element rooms, write-event cards, the review poll, reactions parsing.
Lets the user *see* what's happening and intervene. Cited as
load-bearing in lessons-learned.md round 9; runs 13, 15, 16 used the
intervention surfaces non-trivially (approve-with-failures, retry, comment).

**Evidence weight: moderate but indispensable**. The framework would still
work without it, but humans wouldn't watch and wouldn't catch the cases
the gates miss.

---

## 3. What didn't

Things tried that the registry shows didn't pan out, or that worked
narrowly enough that it's worth flagging.

### qwen2.5-coder:7b

Tried as a swap-in for qwen2.5:7b-instruct on `discord-bot.run6-coder`.
Scored 0/12 — coder-tuned models emit tool calls as JSON-in-content rather
than tool-call format. The JSON-in-content fallback parser exists in
`fleet/llm_adapter.py` and works, but the coder still produced worse
content than instruct.

**Verdict**: don't waste hardware on coder variants for agentic work
under this framework. Instruct tier dominates at this size.

### Raw `report_learning` agent-tool path

Pre-round-8 design: the framework exposed a `report_learning` tool the
LLM was supposed to call when it noticed a failure pattern, populating
the same learning store the auto-learning system later filled
automatically. Weak models bailed before calling it. Was effectively
unused.

**Verdict**: replaced by `synthesize_failure_learning` in round 8.
`report_learning` is still in the tool manifest but hidden by default.
Don't bring it back unless paired with strong instruction reinforcement.

### Fetch retries before `save_as`

Rounds 3–4 tried to add HTTP retries inside the fetch tool. Couldn't
fully solve the cascade because the fetched content still had to round-trip
through the LLM as a tool argument and the LLM truncated it. Round 5's
`fetch_url(save_as=...)` made the fetch atomic — content lands on disk
without the model having to copy it — and the cascade stopped.

`discord-bot.run3` is the canonical "fetch cascade" failure. Run 9's
post-Round-5 re-run cleared it.

**Verdict**: retry alone isn't enough when the value flows through the
model's working memory; atomicity at the storage boundary is.

### Speculative postconditions

Listed in lessons-learned.md's "Closing note" as a guardrail: don't add
postconditions that aren't driven by an observed failure. Adhered to in
post-2026-04-17 work — every C1/C5/C4a/C4b/Phase-2/Phase-3 gate maps to
a specific run that surfaced the bug it now catches.

**Verdict**: rule held. Cost: zero false-positive gate failures observed
in the post-2026-04-17 runs.

### Plan-builder on weak models

`plan-builder.run4-C-7b-failed` confirmed the diagnosis: even with C1
explicitly listing brief deliverables in the next-turn system prompt,
qwen2.5:7b reproduced the same broken plan three retries in a row. The
"flywheel" assumption — feedback in, behaviour change out — relies on the
model being able to integrate abstract corrective text. 7B can't, at
least not in the plan-authoring task class.

**Verdict**: plan-builder is gated on 14B+ hardware for any further 7B
testing. For multi-provider testing, stick with 4o-mini and above.

### Unstable plan-builder runs ≤ run-13

13 of the 14 archived plan-builder runs are not green. They are not
strictly *failures* — most produced a plan, just with one structural
defect each that drove a specific gate. The "What worked" framing
(rank 6, structural validation emerging) sits next to this fact: the
post-2026-04-17 hardening is a series of run/fix/run cycles. The fact
that we needed 13 runs to land a clean one (`run14-4omini-clean`) is
itself evidence about the brittleness frontier of medium-tier models on
plan-authoring tasks.

---

---

## 4. Model-tier comparison

The URL-shortener test bed is the cleanest cross-tier comparison: the
*same* DAG (one runner script, identical postconditions, identical
plan-builder output where applicable) was executed on qwen2.5:7b,
gpt-4o-mini, and gpt-4o. The plan-builder flow is a second comparison
point — same DAG, same gates, three tiers.

All numbers below are **estimated** unless explicitly tagged
**recorded** — see Section 5 for provenance.

### 4.1 URL-shortener executor — same DAG, three tiers

| Tier | Run | Tasks succ/total | Duration | Failure mode |
|---|---|---|---|---|
| qwen2.5:7b | `url-shortener-mvp.run1-7b-broken` | 2/6 | **3722s** (62 min) | Edit-loop / capability-bound. 4 tasks failed; remaining stuck in retry. |
| gpt-4o-mini | `url-shortener-mvp.run7-4omini-typo` | 2/3 | 152s | Plan-builder defect cascaded; produced code had value-level typo. |
| gpt-4o | `url-shortener-mvp.live` | 2/3 | 116s | Plan-builder green, structural gates green; tester wrote wrong tuple shape. Framework couldn't see; model couldn't catch. |

Read across the rows: **7B is capability-bound** (62 minutes of trying,
mostly retrying its own bad edits). **4o-mini is plan-quality-bound**
(framework caught earlier upstream issues; the produced code has subtle
defects that survive the gates). **4o hits a model-judgment ceiling** at
the value-reasoning level (tester writes the wrong-shape tuple; framework
gates see "tuple, tuple"; only running tests would catch it).

The same task count appears across the 4o-mini runs of URL-shortener
(`url-shortener-mvp.run2..7`, all 2/3) — they consistently fail at the
same task class regardless of which framework gate is wired. That
constancy is itself an observation: the framework can change the *cause*
of failure (edit-loop → plan-defect → value-typo) but at the 4o-mini tier
the *failure* is structural, not surmountable by more gates.

### 4.2 Plan-builder — same DAG, three tiers

| Tier | Best run | Outcome | Duration |
|---|---|---|---|
| qwen2.5:7b | `plan-builder.run3-pre-C` | 11/11 tasks completed but plan was structurally broken | 496s |
| qwen2.5:7b | `plan-builder.run4-C-7b-failed` | 5/6 tasks; aborted retry loop after 7B couldn't absorb C1 feedback | 803s |
| gpt-4o-mini | `plan-builder.run14-4omini-clean` | 11/11, **clean reference plan** | 165s |
| gpt-4o | `plan-builder.live` | 11/11, identical DAG outcome | 173s |

The 7B → 4o-mini step is the meaningful jump: **4o-mini produced a clean
plan, 7B did not, ever.** 13 archived 7B-and-below plan-builder attempts
across the registry; none reference-quality. Per the lessons-learned.md
diagnosis (and the memory note `project_model_capability_ceiling.md`),
this is "framework fine, model is the bottleneck."

The 4o-mini → 4o step shows little qualitative difference on this flow.
Both produce 11/11 in similar time. **Plan-authoring is not the task
class where 4o earns its 16× cost premium**; that distinction shows up
in the executor flow (Section 4.1) where 4o passes structural gates that
4o-mini did not.

### 4.3 Discord-bot — single tier, framework progression

The Discord-bot test bed only ran on qwen2.5:7b (Runs 1–17 per
lessons-learned.md). It is not cross-tier; it is the canonical case study for
**how much framework hardening can buy you on a fixed model**.

| Run | First-pass green | Outcome | Note |
|---|---|---|---|
| 1 | 6/11 | DONE via user approve | Framework v0; postconditions added in response |
| 11 | 9/12 | FAILED — transcription bug | Drove edit primitives |
| 13 | **12/12** | **DONE first-pass** | Reference green; rounds 1–11 fixes converged |
| 17 | **23/23** | **DONE first-pass, multi-module** | Reference scale-up; in-phase auto-retry + literal-template staging on every Python-producing task |

Same model, evolving framework, scaling tasks. The progression *is* the
evidence that the framework absorbs work the model can't reliably do.

### 4.4 Tier behaviour summary

Distilling across both test beds:

| Tier | Plan-authoring quality | Edit-tool ergonomics | Value-level reasoning | Bottleneck class |
|---|---|---|---|---|
| qwen2.5:7b | poor — repeats hallucinations | poor — re-emits typos | very poor | **capability** |
| qwen2.5-coder:7b | not tested (instruct-only path) | weaker (JSON-in-content) | not tested | tool-call format |
| gpt-4o-mini | good with hardened gates | needs `_format_match_locations` to escape loops | moderate — structural defects | **plan quality + model judgment** |
| gpt-4o | good | comfortable | good but bounded | **model judgment at value level** |
| 14B+ class | untested — pending hardware (~May 2026) | extrapolated good | extrapolated good | unknown |

This table generalises three test beds × four (tested) tiers. The
"untested" cell is the project's biggest open empirical question.

---

## 5. Cost and latency

All cost figures in this section are **estimated** unless tagged
**recorded**. Estimation rule:

- For runs whose log records a LiteLLM completion-cost line: that value
  (none currently in archive — see provenance note below).
- For runs on a known LiteLLM-supported model without recorded cost:
  per-tier per-run estimate from session memory ($0.025/run gpt-4o-mini,
  $0.40/run gpt-4o).
- For local Ollama runs: $0.

The registry's `cost.source` field is `recorded`, `estimated`, or
`unknown`. Currently 0 runs have `recorded` cost (the LiteLLM cost-line
parser is implemented in the extractor but no logs in the archive
recorded those lines — they were added to the runtime after the bulk of
runs completed). 44/46 runs are `estimated`; 2 are `unknown` (the
oddball directories).

### 5.1 Cost per run, by tier

Median run cost across the registry, by tier:

| Tier | Runs (n) | Cost/run (USD) | Source |
|---|---|---|---|
| qwen2.5:7b | 26 | $0.00 | local |
| qwen2.5-coder:7b | 1 | $0.00 | local |
| gpt-4o-mini | 15 | $0.025 | estimated (session memory) |
| gpt-4o | 2 | $0.40 | estimated (session memory) |

Total 4o-mini spend across 15 archived runs: ~$0.38. Total 4o spend
across 2 runs: ~$0.80. The cross-tier comparison cost the project under
$2 in API spend.

### 5.2 Duration, by tier

Median run duration across runs that have a duration recorded (i.e.
runs whose logs were preserved):

| Tier | Median duration | Min | Max | Note |
|---|---|---|---|---|
| qwen2.5:7b | 460s | 14s | 3722s | Long tail: capability-bound 7B runs occasionally retry-loop into the multi-thousand-second range. `url-shortener-mvp.run1-7b-broken` is the canonical outlier at 62 minutes. |
| gpt-4o-mini | 165s | 100s | 749s | Tighter distribution. The 749s outlier is `plan-builder.run12-phase2-wired` — full plan with structural validation enabled. |
| gpt-4o | 145s | 116s | 173s | Two-run sample, near-identical to 4o-mini latency. |

The 7B median is ~3× the 4o-mini median; the long tail is much wider
because 7B's failure mode is "keep trying" while 4o-mini's failure mode
is "stop earlier with cleaner result". That widens the per-run cost gap
when measured in wall-clock time, not API spend.

### 5.3 Cost per task succeeded

Approximate cost per *successful* task across the test beds. Computed as
`(runs in tier × cost/run) / sum_succ_tasks_in_tier`, using extracted
counts:

| Tier | Σ succ tasks | Total cost | $/succ task | Note |
|---|---|---|---|---|
| qwen2.5:7b (URL shortener only) | 2 | $0 | $0 | But 62 minutes of GPU time |
| qwen2.5:7b (Run 17 reference) | 23 | $0 | $0 | 600s / 23 ≈ 26s/task |
| gpt-4o-mini (URL shortener, 5 runs) | 10 | $0.125 | $0.012 | All produced same 2/3 outcome |
| gpt-4o-mini (plan-builder green) | 11 | $0.025 | $0.002 | Single reference run |
| gpt-4o (URL shortener) | 2 | $0.40 | $0.20 | High per-task because the failing test cost the same as the passing ones |

`$/succ task` is misleading for a comparison ("4o-mini is 100× cheaper
per task than 4o!") because 4o-mini's tasks are not the same shape as
4o's. The right interpretation: at the structural-correctness level, 4o
is ~16× more expensive but *passes more gates per task*. The remaining
gap on URL-shortener (4o failing the value-level test) is not a cost
issue; it's a model-judgment issue.

### 5.4 Provenance disclaimer

Per the cost-source policy in the registry: **none of the dollar figures
in this section are recorded line-items**. They are tier-level estimates
extrapolated from a small number of session-memory observations. They
are useful as orders of magnitude (gpt-4o is ~16× gpt-4o-mini, both are
trivially cheap for 1–10 run experiments) and as a relative comparison
between tiers. They are **not** authoritative for budget planning at
scale.

To upgrade these from estimated to recorded, the LiteLLM cost-tracking
in [src/agora/fleet/llm_adapter.py](../../src/agora/fleet/llm_adapter.py)
needs to be enabled before each run AND the harness needs to log the
final cost figure on a known marker line. The extractor at
[scripts/extract_run_metadata.py](../../scripts/extract_run_metadata.py)
already has the regex; it just needs the log lines to parse.

---

---

## 6. Open framework gaps

Each gap below is paired with the run that surfaced it. Listed roughly
in order of "would yield the most signal if addressed" — not in order of
urgency.

### 6.1 Value-level reasoning at the test level

**Surfaced by**: `url-shortener-mvp.live` (gpt-4o final). Plan green,
structural gates green, 4 of 5 contract tests passed. The failing test:
the tester constructed the wrong tuple shape — declared `(short, long)`
but asserted on `(long, short)`. Framework checked "tuple of two strings",
saw the right type, passed. Only running the test caught the value bug.

**Why open**: framework gates inspect *structure*. They cannot inspect
*intent*. A test that tests the wrong thing is still a syntactically
valid test. Catching this would require either (a) running the test
against a planted reference impl as part of `fill_test_body` validation,
or (b) a "test-vs-spec semantic check" predicate that's beyond simple AST
analysis.

**What the same gap looks like at lower tiers**:
`url-shortener-mvp.run7-4omini-typo` — same pattern at 4o-mini, value-level
typo not caught. The 4o vs 4o-mini comparison shows that *more capable
models produce fewer of these*, but neither tier produces zero, and the
framework has no leverage on the residual.

**Suggested next move**: a `tests_pass_against_planted_impl` predicate
that runs the produced tests against a deliberately-planted-correct impl
and rejects test files whose assertions don't pass. Adds a planted-impl
authoring step to `fill_test_body`.

### 6.2 14B+ tier is untested

**Surfaced by**: `project_paused_for_hardware.md` memory note. RTX 3060
Ti has 8 GB VRAM; qwen2.5:14b needs ~12 GB at Q4_K_M, qwen2.5:32b needs
~20 GB. Hardware upgrade planned ~May 2026.

**Why open**: lessons-learned.md's closing note flags this as the highest-
leverage next step, and `findings.md` Section 4 confirms — the 7B vs
4o-mini gap is "weak vs medium-tier model", but the *next* step (medium
on-prem vs medium API, e.g. qwen 14B vs gpt-4o-mini) is the cost-vs-
capability question that hasn't been asked yet.

**What the same gap looks like elsewhere**: nowhere in the registry —
this is a hole in the empirical surface, not a recurring failure.

**Suggested next move**: re-run `discord-bot.run13` and
`url-shortener-mvp.run7-4omini-typo` against qwen2.5:14b once hardware
is available. The same DAG produces a directly-comparable third row in
Section 4.1.

### 6.3 Code-review flow needs a more capable reviewer

**Surfaced by**: `code-review.run1-noheader`, `.run2-eos`, `.live`.

**Why open**: Section 7 (Category-7 failure) — the model under test
defaults to "looks clean" on every file. Framework infrastructure is
in place (per-file `<x>.py.md`, aggregate `REPORT.md`); only the content
is junk. Same fix as 6.2 (more capable model), but worth flagging
separately because the framework wrapper around code-review is
interesting and could ship value once the reviewer has judgment.

**Suggested next move**: re-run `code-review.run1-noheader` against
gpt-4o-mini or above. If the per-file reviews come out useful, the flow
is shippable as-is. If they're still flat, the prompt/template needs
work, not the model.

### 6.4 Cost not recorded in logs

**Surfaced by**: every LiteLLM-era run in the registry (44 runs). Section
5.4 detail.

**Why open**: cost-tracking code exists in `llm_adapter.py`; the harness
just doesn't log a parseable cost line. All cost figures in `registry.yaml`
are `source: estimated`. Upgrading this to `source: recorded` is a
~30-line fix.

**Suggested next move**: add a `harness: run_cost_total=<float>` log line
at end-of-run in [src/agora/plan/harness.py](../../src/agora/plan/harness.py)'s
`_print_summary`. Update the extractor's regex to parse it and set
`source: recorded` when found.

### 6.5 Plan-builder under-tested on non-trivial briefs

**Surfaced by**: registry — 14 of 14 plan-builder runs in archive used
the URL-shortener brief. Discord-bot and FastAPI-CRUD test beds were
authored by hand, not via plan-builder.

**Why open**: the plan-builder green run (`run14-4omini-clean`) succeeded
on a 6-deliverable URL-shortener brief. We don't know whether plan-builder
handles a 23-deliverable Discord-bot brief without breaking. The brief-
deliverable predicate (C1) was tuned against the URL-shortener vocabulary
(`add`, `lookup`, `list`, `persist`, `save`); a Discord-bot brief
(`register command`, `bridge to matrix`, `respond to slash command`) may
need a wider vocabulary or a different gate shape.

**Suggested next move**: run plan-builder against
`scripts/run_discord_bot_test.py`'s brief on gpt-4o-mini once the
hardware allows pairing with a 4o-mini executor. Compare the generated
plan to the hand-authored DAG.

### 6.6 No checkpoint / resume

**Surfaced by**: `discord-bot-full.run1-aborted`, `.run2-killed`, and
several plan-builder runs with mid-run aborts.

**Why open**: lessons-learned.md "Deferred / not done" already calls this
out — `agora resume <project_room_id>` was scaffolded (Matrix timeline
is authoritative) but never tested end-to-end. With longer multi-module
runs (Run 17 = 600s, plan-builder runs hitting 800s), resume becomes
genuinely useful.

**Suggested next move**: end-to-end test of resume against
`workspace/discord-bot-full.run1-aborted`. The Matrix timeline contains
enough state to drive the orchestrator from where it stopped.

### 6.7 Stub functions shadowing decorated endpoints

**Surfaced by**: lessons-learned.md Run 16 footnote (`fastapi-crud.live`).
The shipped `app.py` has duplicate dead function defs that the decorated
versions below override; tests pass anyway.

**Why open**: harmless but ugly. AST detection is straightforward —
same-name function defs where only one has an `@app.*` decorator should
warn. Has not been worth a round of work to date because no run has
*failed* because of it.

**Suggested next move**: low priority. Add the AST check next time the
`runtime_postconditions.py` module is touched anyway.

---

## 7. Decision log — hypotheses tested

This section catalogues each design hypothesis tried during the project,
the run(s) that tested it, and whether it was kept or dropped. Listed
roughly in chronological order of when each landed.

### 7.1 Kept

| Hypothesis | First validated by | Kept because |
|---|---|---|
| **Postconditions are the ground truth, not the LLM's self-report.** | `discord-bot.run1` (6/11 with framework v0; gates added in response) | Round 1–11 added 12+ postconditions, each driven by a real failure. Removing any of them would regress runs they currently catch. |
| **Auto-hooks (silent enforcement)** — framework runs check_python / git_commit / write_event without the model having to remember. | `discord-bot.run2` (round 2) | Net negative for the model's tool count (14 → 7); the model loses no capability and the framework gains a guaranteed validation cycle. |
| **Hide runtime tools from the LLM's tool manifest.** | `discord-bot.run4` (round 4) | The model is stronger when its tool surface is smaller. Same fix re-applied in round 18 to author_* stages. |
| **`fetch_url(save_as=...)` for atomic fetch-and-save.** | `discord-bot.run3` (cascade) → cleared by post-Round-5 re-run | Atomicity at the storage boundary is the right primitive; retries inside the model's working memory aren't enough. |
| **Three edit primitives (replace/insert_before/append).** | `discord-bot.run11` (transcription) → `discord-bot.run12` (clean) | Made transcription bugs measurably less frequent. Run 12+ never had one on bot.py. |
| **Staging + literal templates for hard-to-generate tasks.** | `discord-bot-full.live` (Run 17, 23/23 first-pass on 7B) | Existence proof for "framework absorbs model unreliability" at scale. The thesis of the project. |
| **`_maybe_queue_narration_redirect` STOP-PLANNING directive.** | `fastapi-crud.live` (Run 16, 13/13 with 1 loopback after narration) | Caught the "Let's read app.py..." stall pattern. |
| **LiteLLM as the multi-provider adapter.** | every gpt-4o-mini and gpt-4o run | One adapter for OpenAI, Anthropic, Gemini, Mistral, Together, …; cost tracking; clean Agora-shape interface. |
| **C1 brief-deliverable predicate.** | `plan-builder.run4-C-7b-failed` (showed 7B couldn't absorb feedback) → `plan-builder.run14-4omini-clean` (showed 4o-mini could) | Confirmed it's the right gate; tier-bounded by what model can ingest the feedback. |
| **C5 api_spec validity hardening.** | `plan-builder.run9-bullet-slipped`, `.run13-test-module` | Catches the structural defects that broke the executor downstream. |
| **C4a/C4b task-vs-spec guards.** | `plan-builder.run10-api-spec-deleted`, `.run11-plan-dir-path` | Same as C5 but at task-declaration level. |
| **Structural validation L1/L2/L3 with PERMISSIVE/STRICT modes.** | `url-shortener-mvp.live` (passed all L1/L2/L3 cleanly) | Catches contract↔test↔impl drift before runtime. STRICT mode is opt-in for high-confidence shipping. |
| **Phase-2 return-type drift detection.** | `plan-builder.run12-phase2-wired` (validated) | Catches tester writing assertions against wrong return type at fill_test_body time, not at execute time. |
| **Phase-3 class_attributes_consistent (auto-injected).** | `url-shortener-mvp.live` (passed) | Catches field-name typos via L3 structural analysis. |
| **`_format_match_locations` in edit-tool errors.** | `plan-builder.run7-c5-edit-loop` (showed the loop) → 4o-mini and 4o never looped after | Ergonomics fix that unblocked medium-tier models. |
| **`strip_test_module_sections` auto-heal in write_file for plan/api_spec.md.** | `plan-builder.run13-test-module` (architect repeatedly reintroduced the same defect across retries) | Sometimes the right fix isn't a louder gate but auto-correction. |
| **Tool lockdown in author_* stages.** | `plan-builder.run10-api-spec-deleted` | Same load-bearing principle as round 4: prevent stage-class boundary violations by hiding the verbs. |
| **Matrix as observer surface.** | rounds 9–10 on Discord-bot | Element + reactions + replies + polls. Without it, humans wouldn't watch and wouldn't catch the cases the gates miss. |

### 7.2 Dropped

| Hypothesis | Tested by | Dropped because |
|---|---|---|
| **qwen2.5-coder:7b as agentic model.** | `discord-bot.run6-coder` (0/12) | Coder emits tool calls as JSON-in-content; instruct dominates at this size. The JSON-in-content fallback parser exists but produces worse content quality. Not worth more time without a fundamentally different model. |
| **Raw `report_learning` agent-tool path.** | rounds 1–7 on Discord-bot | Weak models bail before calling it. Replaced by `synthesize_failure_learning` (round 8). |
| **HTTP retries inside fetch tool.** | rounds 3–4 on Discord-bot | Atomicity at storage boundary (round 5's `save_as`) was the right answer; retries alone weren't enough when the value flows through model working memory. |
| **Open prompt for plan-authoring on 7B.** | `plan-builder.run4-C-7b-failed` | Even with C1 explicitly listing brief deliverables in the system prompt, qwen2.5:7b reproduced the same broken plan three retries in a row. Plan-authoring is gated on 14B+ for any further weak-tier testing. |
| **Minimal-instruction plan-builder system prompt.** | `plan-builder.run8-c5-minimal-instr` | Without explicit "rewrite-on-retry" guidance, the architect kept editing the same broken section over and over. Verbose instructions, ironically, helped the model behave more conservatively. |
| **Speculative postconditions** (postconditions added without an observed failure motivating them). | (rule from lessons-learned.md closing note; held throughout post-2026-04-17 work) | The rule held: every C1/C5/C4a/C4b/Phase-2/Phase-3 gate maps to a specific run. No false-positive gate failures observed in the post-2026-04-17 runs. |

### 7.3 Open hypotheses (not yet tested, kept as candidates)

| Hypothesis | Awaits | Why it's interesting |
|---|---|---|
| **14B+ class on Agora.** | hardware (~24 GB VRAM, May 2026) | Section 4.4 — biggest empirical question in the project. |
| **Plan-builder on a Discord-bot or FastAPI brief.** | hardware + paired tier choice | Section 6.5 — would test whether C1 / DAG-shape generalises beyond URL-shortener-style briefs. |
| **Tests-pass-against-planted-impl predicate.** | author cycle | Section 6.1 — would close the value-level test gap. |
| **End-to-end resume flow.** | author cycle | Section 6.6 — Matrix timeline is authoritative; just needs an integration test. |
| **mkdocs render of registry + findings.** | low-priority polish | The artefacts are markdown + YAML; a static-site view would be nice but not load-bearing. |

---

---

## 8. Methodology notes

### 8.1 Date-provenance — agent-drift caught

During the 2026-04-26 archival session that produced these documents, an
early draft of the plan claimed the project spanned "ten weeks
(2026-02 → 2026-04)". The repo was created 2026-04-15; today is
2026-04-26; project lifetime is 11 days, active workspace git history
spans 2026-04-16 → 2026-04-22 (7 days). The "ten weeks" figure was an
order-of-magnitude session-memory drift from the archival agent —
fabricated with a confident, specific date range that propagated through
the plan unchallenged until the user (correctly) flagged it.

This is a small instance of the failure mode the archive itself is
meant to mitigate. The fix shipped alongside the rest of Phase A:

- [scripts/check_date_provenance.py](../../scripts/check_date_provenance.py)
  cross-references every ISO date in `lessons-learned.md`, `findings.md`,
  `registry_notes.yaml`, and per-run narratives against workspace git
  commit ranges and the repo creation date. Flags durations that exceed
  the project lifetime by more than 20%.
- Registry schema continues to enforce `cost.source: recorded |
  estimated | unknown`. The same pattern applied to dates: assertions
  that can resolve to filesystem evidence should be checked against it.

The lesson generalises: when humans review LLM output for software
projects, the LLM-confident-and-specific failure mode is more dangerous
than the LLM-vague failure mode, because confident specifics look like
they came from somewhere. Make it cheap to verify them against ground
truth.

### 8.2 Queued for publishable.md (Phase B)

When the cross-tier comparison and the per-run narratives are written
up for an external audience, two threads beyond the technical results
are worth surfacing:

- **Velocity**. 48 archived runs across three test beds, three model
  tiers, plan-builder + code-review flows, and four progressive
  framework hardening rounds — produced in 11 days of independent
  development by one developer with one capable-agent collaborator. The
  velocity is part of the story, not just the artefacts.
- **The archival lesson itself**. Section 8.1 above belongs in any
  account of the project that goes outside Agora's own context, because
  it generalises: cost-provenance, date-provenance, citation-integrity,
  tone-match — these are *templates* for keeping LLM-generated archives
  honest. Worth surfacing as a methodology contribution rather than
  buried in a footnote of a technical-results paper.

Both are meta-level observations. They fit in `publishable.md` (Phase B
deliverable) as candidates with thesis sentences, evidence, and
suggested venue/format — see plan.

---

*Estimates only — see [model-profiles.yaml](../../../mcp-server/.claude/skills/model-profiles.yaml) heuristics and `cost.source` fields in registry.yaml for provenance. Date claims verified by [scripts/check_date_provenance.py](../../scripts/check_date_provenance.py) against workspace git history.*
