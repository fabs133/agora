# `plan-builder.run14-4omini-clean`

**Reference plan-builder green run.** All C1 / C4a / C4b / C5 / Phase-2
gates active, structural validation in place. The plan came out clean,
the executor would consume it without structural objections, and the
total spend was ~$0.025.

| Field | Value |
|---|---|
| Model | gpt-4o-mini (OpenAI via LiteLLM) |
| Project | plan-builder |
| Date | 2026-04-22 19:46:00 → 19:48:45 (~165s wall-clock) |
| Tasks succ/total | **11/11** |
| Loopbacks | 0 |
| Cost | ~$0.025 (estimated, source: session memory) |
| Logs | [planner_phase23b.log](../../workspace/.logs/planner_phase23b.log), [voter_phase23b.log](../../workspace/.logs/voter_phase23b.log) |
| Run dir | [workspace/plan-builder.run14-4omini-clean](../../workspace/plan-builder.run14-4omini-clean/) |

## Setup

The plan-builder flow is an 11-task DAG that *builds* a v2.0 executor
plan from a brief: gather context, decide library + storage, author
api_spec, draft tasks, vote, emit. It runs through `run_plan.py` and
emits `plan/out.plan.yaml` consumable by another `run_plan.py` invocation
as the executor.

The brief was the same six-deliverable URL shortener used across the
URL-shortener executor comparisons — a natural choice for the cross-tier
comparison since both planner-side and executor-side data exist for it.

The framework was at full post-2026-04-17 maturity:

- **C1** brief-deliverable predicate (`api_spec_covers_brief_deliverables`)
  with the `_BRIEF_VERB_KEYWORDS` map.
- **C4a/C4b** `_validate_src_path_in_api_spec` covering any non-test
  `.py` file; `_align_impl_tasks_to_spec` raising on would-drop.
- **C5** `api_spec_is_valid` rejecting duplicate module headers,
  parse failures, stray top-level statements (`_find_stray_top_level_statements`).
- **Phase 2** return-type drift detection in `fill_test_body`.
- **Phase 3** `class_attributes_consistent` auto-injected by
  `_auto_inject_class_attrs_consistency`.
- Tool lockdown in author_* stages: `write_file`, `delete_file`,
  `edit_file_*`, `fetch_url`, `add_class*`, `add_function*` all hidden.
- `strip_test_module_sections` auto-heal on `write_file('plan/api_spec.md', …)`.
- `_format_match_locations` in edit-tool errors so the model can find
  unique anchors.

## What happened

The 11 tasks completed in a tight ~165 second window. From the log:

```
19:46:00  init -> analysis
19:46:06  task gather_context done: success=True iterations=3
19:46:36  task review_brief done: success=True iterations=0
19:47:09  task decide_library done: success=True iterations=0
19:47:40  task decide_storage done: success=True iterations=0
19:47:48  task research_library done: success=True iterations=4
19:47:52  task define_api done: success=True iterations=3
19:48:00  task author_agents done: success=True iterations=6
…
19:48:20  task author_tasks done: success=True iterations=12
19:48:20  task validate_agents_vs_tasks done: success=True iterations=0
19:48:20  task finalize_plan done: success=True iterations=0
19:48:45  task review_plan done: success=True iterations=0
```

Even on the *clean reference* run, **C4a fired twice and the architect
absorbed the feedback both times.** From within the `author_tasks` task:

```
20:48:05  tool call: task=author_tasks:author_core_module turn=1
          name=plan_add_task_spec args={'task_id': 'core_domain_module',
          …'output_path': 'src/core_domain.py'}
          → ERROR: task 'core_domain_module' references Python module
          path(s) not declared in plan/api_spec.md:
          - output_path: 'src/core_domain.py'

20:48:07  tool call: task=author_tasks:author_core_module turn=2
          name=post_note args={'body': 'It seems that the
          src/core_domain.py module is not declared in the
          plan/api_spec.md file. We need to update…'}
          result=note posted

20:48:13  tool call: task=author_tasks:author_cli_logic turn=1
          name=plan_add_task_spec args={'task_id': 'cli_entry_point',
          …'output_path': 'src/cli.py'}
          → ERROR: task 'cli_entry_point' references Python module
          path(s) not declared in plan/api_spec.md:
          - output_path: 'src/cli.py'

20:48:15  tool call: task=author_tasks:author_cli_logic turn=2
          name=report_progress args={'message': "The CLI module path
          'src/cli.py' is not declared in the api_spec. I will need to
          retarget this task to one  result=progress reported"}
```

The architect tried to declare two impl tasks against module paths
(`src/core_domain.py`, `src/cli.py`) that weren't in the api_spec
(`src/url_shortener.py` was, plus `src/utils.py`). C4a refused both. The
architect read the error, posted a note acknowledging the mismatch on
the first, and `report_progress`'d its retargeting plan on the second.
By the end of `author_tasks` (12 iterations) the impl tasks were
retargeted onto declared modules and the run continued.

The produced api_spec shows two clean modules:

```python
# API spec

## module: src/url_shortener.py

class URLShortener:
    def __init__(self) -> None: ...
    def add_url(self, long_url: str) -> str: ...
    def lookup(self, short_hash: str) -> str: ...
    def list_mappings(self) -> list[tuple[str, str]]: ...
    def save(self, path: str) -> None: ...
    def load(self, path: str) -> None: ...

## module: src/utils.py

def generate_short_hash(long_url: str) -> str: ...
```

No duplicate module headers, no prose, no test modules — C5 wouldn't
have anything to flag.

The plan was emitted as `plan/out.plan.yaml`; the voter (auto-vote
script in
[scripts/auto_vote_plan_builder.py](../../scripts/auto_vote_plan_builder.py))
approved the brief, library decision (stdlib-only), storage decision
(in-memory), and final plan, captured in `decision_*.txt` files in the
run dir.

## What worked

- **C4a as a real feedback signal.** Two retargeting events, both
  absorbed by the architect on the next turn. Compare against
  `plan-builder.run4-C-7b-failed` where the same C-class feedback
  produced no behaviour change across three retries — the gap between
  4o-mini and 7B is exactly visible here.
- **Tool lockdown on author_* stages.** No `write_file` to `src/`, no
  `delete_file` on `plan/api_spec.md`. The architect could only
  `plan_add_task_spec`, `post_note`, `report_progress` during this
  stage, and that was enough.
- **Decision-stage auto-vote.** Runs through `decide_library`,
  `decide_storage`, `review_brief`, `review_plan` in
  `iterations=0` — the human-in-the-loop steps don't slow the test
  cycle when the voter's there to approve.

## What broke

Nothing in this run. C4a fired but didn't break anything — the architect
recovered. That's the run reaching the asymptote.

The fact that C4a *did* fire even on the reference green run is itself
evidence that the gate is load-bearing. Removing C4a would have produced
a plan that referenced undeclared modules and the executor downstream
would have failed.

## What changed in the framework as a result

This run is the *evidence* that the post-2026-04-17 hardening converged.
Specifically:

- It is the existence proof that C1 + C5 + C4a + C4b + Phase 2 + Phase 3
  + tool-lockdown + auto-strip jointly produce a clean plan from a
  6-deliverable brief on gpt-4o-mini in ~$0.025 of API spend.
- It seeded the gpt-4o cross-tier comparison
  ([url-shortener-mvp.live](url-shortener-mvp.live.md)) — same brief,
  same DAG, executor side on the next-tier-up model.
- It is the reference green for the plan-builder column in
  [findings.md §4.2](findings.md).

What this run did *not* validate, and what is still open per
[findings.md §6.5](findings.md), is whether plan-builder generalises
beyond the URL-shortener brief. The C1 verb dictionary
(add/lookup/list/persist/save) was tuned against this brief; a
Discord-bot brief would need a different vocabulary.

## See also

- [findings.md §4.2](findings.md) — plan-builder cross-tier comparison;
  this run is the 4o-mini row.
- [findings.md §6.5](findings.md) — open question: plan-builder on
  non-trivial briefs.
- [findings.md §7.1](findings.md) — decision log entries for C1, C4a,
  C4b, C5, Phase 2, Phase 3 — every one of which fired or was relevant
  on this run.
- [registry.yaml](registry.yaml) `runs[*].run_id ==
  "plan-builder.run14-4omini-clean"`.
- [url-shortener-mvp.live.md](url-shortener-mvp.live.md) — the executor
  run that consumed a plan structurally similar to the one this run
  produced.
- [lessons-learned.md](../lessons-learned.md) "Post-2026-04-17 work" Round
  table — every gate referenced above maps to a Round 14–18 row.
