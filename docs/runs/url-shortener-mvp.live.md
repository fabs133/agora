# `url-shortener-mvp.live` (gpt-4o run)

**Cross-tier comparison anchor.** Same DAG as
[run1-7b-broken](url-shortener-mvp.run1-7b-broken.md), same brief, on
gpt-4o. Structural gates passed; framework hit its ceiling at the
value-level test bug.

| Field | Value |
|---|---|
| Model | gpt-4o (OpenAI via LiteLLM) |
| Project | url-shortener-mvp |
| Date | 2026-04-22 20:01 → 20:03 (~2 min wall-clock) |
| Tasks succ/total | 2/3 (1 fail) — but with implement_core_domain_module looping internally to iter=16 before exiting |
| Loopbacks | 0 (review phase entered after one task escalated via `request_review`) |
| Cost | ~$0.40 (estimated, source: session memory) |
| Logs | [executor_gpt4o.log](../../workspace/.logs/executor_gpt4o.log), [planner_gpt4o.log](../../workspace/.logs/planner_gpt4o.log), [voter_gpt4o.log](../../workspace/.logs/voter_gpt4o.log) |
| Run dir | [workspace/url-shortener-mvp/](../../workspace/url-shortener-mvp/) (base "live" dir) |

## Setup

Same six-deliverable URL shortener brief as the 7B baseline. The plan
that drove this run was emitted by a parallel gpt-4o plan-builder run
(`plan-builder.live`) — both planner and executor on gpt-4o, driving the
same DAG that 4o-mini and 7B had attempted.

The framework was at its post-2026-04-17 maturity: C5 api_spec validity,
C4a/C4b task-vs-spec guards, Phase-2 return-type drift detection in
`fill_test_body`, Phase-3 `class_attributes_consistent` auto-injected
onto every impl task with `py_compiles(src/*.py)`. Visible in the loader
prelude:

> `loader: auto-injecting class_attributes_consistent(rel='src/url_shortener.py') on task 'implement_cli_entry_point'`

## What happened

The run executed in two minutes wall-clock — fast. Three tasks ran:

1. **`setup_project`** (3 turns, success). The architect started by
   trying to write to `src/url_shortener/__init__.py` with content; that
   collided with an existing file (an artifact of an earlier run on the
   same dir). The framework reported the collision; the architect
   adjusted and wrote `src/url_shortener/shorten.py` instead. Soft
   warnings on path mismatch fired but didn't block. Auto-hook git_commit
   reported `FAIL` on a couple of writes ("nothing to commit") — that's
   the post-Round-12 try/except wrap absorbing them silently. Get-pip.py
   was downloaded and check_python rejected it; the model also produced
   a `setup.py` that failed `run_python_import` — see "What broke" below.

2. **`pytest_tests_contract`** (7 turns, success). The contract test
   stage went through three sub-stages: scaffold, derive_intent,
   fill_assertions. On the first fill_assertions attempt, the tester
   called `URLShortener.add(url)` and `URLShortener.lookup(hash)` in
   five separate `fill_test_body` calls. **Phase-2 / contract gate caught
   every one**:

   > `ERROR: tool fill_test_body raised: fill_test_body: your code calls
   > methods that are NOT in the api_spec — URLShortener.a[dd is not in
   > the contract; valid methods are URLShortener.add_url, …]`

   The tester recovered on turn 2 by calling
   `recall_knowledge('url shortener methods')` — the right move. By turn
   3 it had the right method names and filled the test bodies cleanly.

3. **`implement_core_domain_module`** (success=False iter=9, success=False iter=16, success=False iter=13). The impl task looped three times.
   The first impl had a `class_attributes_consistent` failure. The model
   adjusted, tried again, hit a different gate, eventually called
   `request_review` to escalate to the user. The phase advanced through
   architecture → implementation → testing → review.

Net outcome: 2 of 3 user-visible tasks green; the impl task escalated
via `request_review` rather than failing outright. Reading the produced
code (28 lines of `url_shortener.py`) shows that `add_url`, `lookup_url`,
`list_mappings`, `save`, `load` all exist with sensible bodies.

## What worked

- **Phase 2 caught five tester hallucinations on turn 1.** The tester
  invented method names (`add`, `lookup` instead of `add_url`,
  `lookup_url`); the gate refused to fill the test bodies; the tester
  read the contract again and got it right by turn 3. This is the gate
  doing exactly what it was designed to do.
- **Phase 3 caught field-name issues on impl iterations.**
  Auto-injected via `_auto_inject_class_attrs_consistency`, the predicate
  fired on early impl drafts and gave the model a precise correction.
- **`request_review` as an exit ramp.** When the impl task wasn't
  converging cleanly, the model escalated rather than looping forever.
  The phase advanced — the user gets to inspect the artefacts rather
  than the framework hanging.
- **Soft warnings, hard errors.** `path mismatch` warnings on
  `requirements.txt` vs `src/url_shortener/__init__.py` fired but
  didn't block. The collision check on existing files DID block.
  Right ergonomics for a higher-tier model.

## What broke

- **Workspace pollution.** The architect downloaded `get-pip.py` (65 KB)
  and `pytest.zip` (65 KB) into the project root, presumably trying to
  install pytest as part of "scaffolding". Both files are still in
  [workspace/url-shortener-mvp/](../../workspace/url-shortener-mvp/).
  The framework didn't stop these — `fetch_url(save_as=...)` is a
  legitimate primitive and the architect's call shape was valid. Just
  not what the brief asked for.
- **Setup.py side effect.** Same impulse — gpt-4o tried to make this a
  "real" Python package. The setup.py failed `run_python_import` and
  was committed anyway.
- **Value-level test bug not caught.** The shipped
  `list_mappings` returns `[(url, hash) for hash, url in ...]` — a tuple
  in `(long, short)` order. Whether that's correct depends on the
  tester's expectations; per session memory the tester constructed the
  wrong-shape tuple, the test failed at value-level, and the framework
  could not see it. **The framework's structural gates can't inspect
  test intent.** This is the cleanest example of [findings.md §6.1](findings.md)
  — value-level reasoning at the test level, the open framework gap.

## What changed in the framework as a result

This run did not drive a *new* gate — every gate that fired was already
in place from rounds 14–18. What it produced was:

- **Validation that L1/L2/L3 + Phase 2 + Phase 3 work as designed at the
  gpt-4o tier.** Every gate caught what it was meant to catch.
- **Empirical confirmation of the framework ceiling.** 4 of 5 contract
  tests passed; the 5th failed because of test-vs-impl value disagreement
  the framework cannot detect. This is the practical edge of what the
  current gates can extract from a sub-Sonnet model — an empirical
  benchmark for whether a future "tests-pass-against-planted-impl"
  predicate would be worth building.
- **Workspace pollution as a friction point.** Future work could either
  add a `forbidden_paths` postcondition (no `get-pip.py`, no top-level
  `setup.py` unless the brief asked for one) or accept it as
  capable-model exuberance and clean up after.

## See also

- [findings.md §4.1](findings.md) — cross-tier comparison row for this
  run.
- [findings.md §6.1](findings.md) — the value-level reasoning gap this
  run exemplifies.
- [findings.md §7.1](findings.md) — Phase-2 + Phase-3 + structural L1/L2/L3
  decision-log entries that were validated here.
- [registry.yaml](registry.yaml) `runs[*].run_id == "url-shortener-mvp.live"`.
- [url-shortener-mvp.run1-7b-broken.md](url-shortener-mvp.run1-7b-broken.md)
  — same DAG, qwen2.5:7b, 62 minutes for 2 tasks.
- [plan-builder.run14-4omini-clean.md](plan-builder.run14-4omini-clean.md)
  — the plan-builder green run that produced the predecessor of the
  plan this gpt-4o run consumed.
