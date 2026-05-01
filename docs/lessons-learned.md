# Agora — Project log & lessons learned

This document is the round-by-round project log: architecture, the five
load-bearing ideas, what failed and why, and the round table of fixes
that drove each gate from a real run.

**Layered snapshots**: the bulk of this file is the **original
2026-04-17 snapshot** covering Runs 1–17 on `qwen2.5:7b-instruct` (a
three-day active testing window per the workspace git history, despite
"stress-test week" framing in the prose). The **Post-2026-04-17 work**
section near the bottom captures the nine days that followed
(2026-04-18 to 2026-04-26): multi-provider adapter via LiteLLM, the
plan-builder meta-flow, and structural-validation hardening (Rounds
14–18).

**Run history archive**: structured per-run records and cross-cutting
analysis live under [runs/](runs/) — `registry.yaml` is the single
source of truth, `findings.md` synthesises themes, `README.md` indexes
the lot.

**Date**: 2026-04-17 (end of stress-test week, Runs 1–16)
**Model under test**: `qwen2.5:7b-instruct` (Q4_K_M, local Ollama, RTX 3060 Ti 8 GB)
**Test coverage**: 501 tests passing, 82.17% coverage
**Status**: Framework stable. Delivered working artifacts in two distinct domains
(Discord bot, FastAPI CRUD) via a weak local LLM.

---

## What Agora is

A multi-agent orchestration framework that takes a weak local LLM and a DAG of
specified tasks, and drives the LLM to produce working code end-to-end. The
pattern: **make the framework absorb everything the model is unreliable at, so
the model only does what it's good at (generating small focused payloads)**.

The architecture is built around the **Manifold Specification Pattern** — every
task carries preconditions and postconditions, and the framework enforces them
as deterministic gates. Matrix is the event bus + human-observer surface; Git
is the artifact store; Ollama serves the models.

---

## Architecture at a glance

```
                           ┌───────────────────────┐
                           │  run_<project>_test.py │  ← runner script
                           │  (task DAG + staging)  │
                           └───────────┬───────────┘
                                       │
                                       ▼
           ┌──────────────────── Orchestrator ────────────────────┐
           │                                                      │
           │   ┌─ AgentRuntime ─┐       ┌─ StageRunner ─┐          │
           │   │  (execute_task)│       │  (staged: N ) │          │
           │   └──────┬─────────┘       └──────┬────────┘          │
           │          └──── shared _run_loop ──┘                   │
           │                   │                                  │
           │                   ▼                                  │
           │            Tool-call loop                            │
           │             │                                        │
           │             ├─▶ inner tools (write/read/edit/fetch)  │
           │             ├─▶ auto-hooks (validate + git_commit)   │
           │             ├─▶ write-event cards posted to observer │
           │             │                                        │
           │             └─ on exit: synthesize mark_complete,    │
           │                 run postconditions, record auto-     │
           │                 learnings, detect narration          │
           └──────────────────────────┬───────────────────────────┘
                                      │
                                      ▼
                   ┌──────── Observer layer ─────────┐
                   │   Matrix rooms (Conduit)         │
                   │   Renderer → Element cards       │
                   │   EventDispatcher (reactions,    │
                   │   replies, /agora commands)      │
                   │   ReviewCoordinator (MSC3381     │
                   │   poll + artifact snapshot)      │
                   └──────────────────────────────────┘
```

50 Python modules under `src/agora/`, 62 test modules under `tests/`. Core
packages:

- **`core/`** — domain types (Task, Specification, Predicate, Project, Learning).
  Pure, no I/O. Postconditions live here.
- **`fleet/`** — the orchestration engine. Everything the LLM touches flows
  through this layer.
- **`matrix/`** — matrix-nio wrapper + SSRF-safe HTTPS fetcher.
- **`observe/`** — Element-side UX: formatters, polls, renderer, review,
  command parser.

---

## The five load-bearing ideas

Each of these solved a real failure mode observed in live runs. Remove any
one and the framework regresses toward "just a 7B model trying its best".

### 1. Postconditions are the ground truth, not the LLM's self-report

`Specification.postconditions` is a tuple of `Predicate`s evaluated after each
task runs ([core/contract.py:57–72](../src/agora/core/contract.py#L57-L72)).
A task is `success` iff **every postcondition evaluates True**. The LLM's
`mark_complete` call is observed but doesn't determine outcome.

The framework ships four families of postconditions:

- **Static** (cheap, no I/O): `_postcond_file_exists`, `_postcond_file_contains`,
  `_postcond_mark_complete`, `_postcond_py_compiles`.
- **Subprocess** (verify running-ness): `postcond_python_imports`,
  `postcond_pytest_passes`, `postcond_requirements_parse` (all in
  [fleet/runtime_postconditions.py](../src/agora/fleet/runtime_postconditions.py)).
- **AST-level** (structural invariants):
  `postcond_no_code_after_main_block` catches handlers placed after
  `if __name__ == '__main__':` (unreachable at runtime).
- **Cross-file consistency**: `postcond_readme_only_references_existing_commands`,
  `postcond_bot_calls_tree_sync`.

### 2. Auto-hooks — framework runs the tools the LLM would forget

After any `write_file`, `fetch_url(save_as=...)`, or `edit_file_*` call,
`run_auto_hooks` ([fleet/auto_hooks.py](../src/agora/fleet/auto_hooks.py)) fires:

- `.py` files → `check_python` → (if clean) `run_python_import`
- `requirements.txt` → `check_requirements`
- successful validation → `git_commit` with message `"auto: wrote <path>"`
- silently: validation errors appended to the LLM's next-turn message
  history so the model sees them without having to remember to call the
  check tool
- silently: **write-event card** posted to the project room so the
  observer watches the build in real time

When `auto_hooks_enabled=True` on `ToolContext`, the tools `check_python`,
`git_commit`, `git_diff`, `git_log`, `mark_complete`, and `report_learning` are
**hidden from the LLM's tool manifest**. The model goes from 14 tools to 7.

### 3. The flywheel: auto-learnings feed the loopback

When a task fails a postcondition, the orchestrator synthesizes a `Learning`
from the exact `(task_id, predicate_name, reason)` tuple
([fleet/auto_learning.py](../src/agora/fleet/auto_learning.py)) and injects it
into the agent's `learned_patterns` list. The next time `_compose_system_prompt`
runs for that agent, the learning appears in a `## Learned context` block in
the system prompt. Dedup is by stable hash of the normalised reason — same
failure twice doesn't duplicate, it reinforces.

This replaced the prior `report_learning` agent-tool path which required the
LLM to self-reflect and often went unused (weak models bail before calling
it).

### 4. Edit primitives — the model never re-emits existing content

Three tools in [fleet/inner_tools.py](../src/agora/fleet/inner_tools.py):

- `edit_file_replace(path, old_string, new_string)` — substitution with
  unique-match requirement (like Claude Code's Edit tool).
- `edit_file_insert_before(path, anchor, snippet)` — line-anchor insertion.
- `edit_file_append(path, snippet)` — append to end.

Replaced the "model re-emits the whole file via write_file" pattern that
consistently introduced transcription errors (`discord.InterACTION` in
Run 11, duplicated `import random`). Run 12 onwards never had a
transcription bug on bot.py.

### 5. Staged tasks + literal templates

For hard-to-generate tasks, the runner splits a Task into a sequence of
`Stage` objects with small `max_iterations` caps and pre-loaded `context_files`
in the user message ([fleet/stage_runner.py](../src/agora/fleet/stage_runner.py)).
Each stage gets a fresh message history so accumulated context doesn't blow
past num_ctx. Hard cases (`write_requirements`, `build_skeleton`) use literal
code templates — *"write EXACTLY this content"* — leaving no room for
hallucination.

---

## Round-by-round evolution

Numbers are concrete: `Runs N/M` = "passed N tasks out of M in that run's
first attempt". Loopback retries sometimes bump the final count.

| Round | Problem observed | Fix added | Result |
|---|---|---|---|
| Pre-1 | Baseline 7-sprint framework | (initial) | Runs but fragile |
| 1 | Tasks FAIL silently, mark_complete misses | Four static postconditions + `_postcond_mark_complete` | Run 1 · 6/11 via user approve |
| 2 | `work_dir ≠ git repo`; `check_python` misses NameError | Unified work_dir + git; AST undefined-name check; DAG reshape so failures don't block documentation tasks | Run 2 · 9/12 |
| 3 | syntactic-valid-but-AttributeError bugs slip past py_compile | Subprocess `run_python_import`, `run_pytest`, `check_requirements` as postconditions | Run 3 · 9/12 FAILED (cascading fetch issue) |
| 4 | Regressed because tools encouraged over-engineering | Hide runtime tools from the LLM, postcondition-only; revert prompts | Stable but fetch still fragile |
| 5 | 67 KB `kb/commands.md` can't be echoed back as a tool arg | `fetch_url(save_as=...)` for atomic fetch-and-save; bounded retry on transient HTTP errors | Run 9 · 10/12 approved |
| 6 | Model writes to wrong path (kb/design instead of design/modules.md) | `Task.output_path` banner in prompt + soft-warn on write_file path mismatch | Run 10 · 10/12 |
| 7 | `read_file kb/commands.md` (67 KB) overflows num_ctx | Hierarchical map-reduce distiller on reads exceeding 8 KB threshold | Run 11 · 9/12 (regression in build_roll edit) |
| 8 | Re-emitting 25 lines of bot.py introduces `discord.InterACTION` typo | Three edit primitives (replace/insert_before/append) | Run 12 · 12/12 via 1 loopback |
| 9 | Review poll is a black box — no file tree, no diff | Artifact snapshot in review summary; write-event cards; per-task `/agora comment` verb | Run 13 · 12/12 zero loopbacks (5.3 min) |
| 10 | No way to vote except type /agora — slow UX | Matrix `m.reaction` parser + threaded-reply → implicit comment routing + pinned command card + review-summary reaction counts | Run 13 ran cleanly with new surfaces |
| 11 | Handler landed after `if __name__:` — tests see it, production doesn't | `_find_code_after_main_block` AST detector + `postcond_no_code_after_main_block` | Catches exactly the Run-13 bug |
| 12 | Stress test in new domain (FastAPI) exposes `nothing to commit` and `/agora review reject` | try/except on `git_commit` in auto-hook; `reject`/`retry`/`ok` aliases | Run 15 · 10/13 approved |
| 13 | Model narrates "Let's read app.py…" instead of calling tool | `_maybe_queue_narration_redirect` detects zero-artifact task with declared `output_path`; queues STOP-PLANNING `[SYSTEM]` directive into task_comments | Run 16 · 13/13 via 1 loopback, **pytest passes on produced code** |

---

## Empirical evidence — the Run log

Two distinct projects built by the same framework on the same 7B model:

| Run | Project | First-pass green | Loopbacks | Outcome | Duration | Tokens |
|---|---|---|---|---|---|---|
| 1 | discord-bot | 6/11 | 0 | DONE (user approved) | — | — |
| 2 | discord-bot | 9/12 | 0 | DONE | — | — |
| 3 | discord-bot | 9/12 | 1 | FAILED (cascade) | — | — |
| 4 | discord-bot | 7/12 | 1 | FAILED (loopback exhaust) | — | — |
| 5 | discord-bot | 9/12 | 1 | FAILED | — | — |
| 6 | discord-bot (qwen2.5-coder:7b) | 0/12 | 0 | Aborted — coder doesn't tool-call | — | — |
| 7 | discord-bot | 7/12 | 2 | FAILED | — | — |
| 8 | discord-bot | 8/12 | 0 | Stopped early | — | — |
| 9 | discord-bot | 9/12 | 2 | FAILED | — | — |
| 10 | discord-bot | 10/12 | 1 | Stopped on confusion | — | — |
| 11 | discord-bot | 9/12 | 2 | FAILED (transcription bug) | 938 s | 300k / 16k |
| 12 | discord-bot | 12/12 | 1 | DONE | 458 s | 54k / 3k |
| 13 | discord-bot | **12/12** | **0** | **DONE** | **316 s** | 47k / 2.4k |
| 14 | fastapi-crud | 9/13 | 2 | FAILED (git_commit bug) | 548 s | 45k / 2k |
| 15 | fastapi-crud | 10/13 | 2 | DONE (approved with failures) | 397 s | 77k / 3.7k |
| 16 | fastapi-crud | **13/13** | **1** | **DONE, pytest passes** | 359 s | 57k / 3.7k |
| 17 | discord-bot-full | **23/23** | **0** | **DONE, pytest 4/4, approved** | 600 s | 80k / 3.9k |

Run 13 (Discord bot, 12/12 zero loopbacks, 5.3 min) and Run 16 (FastAPI CRUD,
13/13 one loopback, 6 min) are the reference runs. Both produce functional
code: `workspace/discord-bot.run13/bot.py` registers all three slash commands;
`workspace/fastapi-crud/app.py` passes its own pytest suite with 2/2 tests
green.

**Run 17 (2026-04-18, discord-bot-full, 23/23, 0 loopbacks, 10 min)** is the
scale-up reference: multi-module (config.py + matrix_bridge.py + bot.py + two
test files), eight slash commands that bridge to Agora's `/agora` command
surface via matrix-nio, and every task passed on its first attempt. 4/4 pytest
green on the shipped code. Produced on the same qwen2.5:7b hardware that
previously hit the capability ceiling on the small runners — the difference
came from (a) the in-phase auto-retry added in the same session, which meant
the framework could absorb two transient flakes that would have needed
cross-phase loopbacks before, and (b) staging every Python-producing task
with a literal template (run 2 of this runner failed on `design_bridge_spec`
after it read a 64 KB docs page and returned empty; staging the markdown body
removed the read entirely). Net takeaway: the 7B capability story improves
dramatically when *every* source-producing task is a template-driven stage.

---

## The 7B capability ceiling — honest assessment

Every failure mode observed in Runs 1–16 falls into one of three categories:

1. **The framework lacked a gate** (Run 1–5, Run 11, Run 14). **Addressed**.
2. **The model hallucinates a fact** (`discord.utils.random`,
   `commands.Intents`, `discord.InterACTION`, `@app.put` before `app = ...`).
   Gates catch the failure; auto-learning records it; loopback retries. Works
   usually. Not always — the model may reproduce the same hallucination on
   retry (seen repeatedly with `import discord` in requirements.txt).
3. **The model narrates instead of acting** (Run 15, Run 16 first pass).
   **Addressed** by `_maybe_queue_narration_redirect` — detected +
   system-authored STOP directive on retry.

What the framework cannot do with a 7B model:

- Guarantee zero transcription bugs on edits. Templates + edit primitives
  reduce the surface but a capability-limited model will still occasionally
  write duplicate imports or stray decorator stacks.
- Absorb abstract corrective advice. "Don't put `import discord` in
  requirements.txt" appears verbatim in the model's system prompt across
  multiple retries and the model still reproduces the bug. The auto-learnings
  inject the traceback but don't guarantee behaviour change.

The file-level evidence is clear: the code that ships is *structurally*
correct even when cosmetically messy. Run 16's `app.py` has duplicate dead
function defs, but the decorated versions below them override and the tests
pass.

A 14B+ model plugged into this framework would almost certainly clear
category 2 and 3 entirely. That's the next-hardware direction (waiting for
~24 GB VRAM per `project_paused_for_hardware.md` in the memory notes).

---

## File layout — where to look

**Runner scripts** (drive projects, not framework code):
- [scripts/run_discord_bot_test.py](../scripts/run_discord_bot_test.py) — 13-task DAG, 7 staged, Discord bot domain
- [scripts/run_fastapi_crud_test.py](../scripts/run_fastapi_crud_test.py) — 13-task DAG, 7 staged, FastAPI CRUD domain
- [scripts/run_discord_bot_full_test.py](../scripts/run_discord_bot_full_test.py) — **stress-test runner**: 23-task DAG, 15 staged, multi-module Discord bot that mirrors Agora's full `/agora` command surface via a matrix-nio bridge. Built after Round 16 when the small runners hit the 7B capability asymptote; waiting on 14B+ hardware before running for real.

**Core framework** (don't touch casually):
- [src/agora/core/task.py](../src/agora/core/task.py) — Task dataclass with `output_path`
- [src/agora/core/contract.py](../src/agora/core/contract.py) — Specification, Predicate, evaluate_postconditions
- [src/agora/core/learning.py](../src/agora/core/learning.py) — Learning dataclass + decay/reinforce
- [src/agora/core/project.py](../src/agora/core/project.py) — Project + phase state machine
- [src/agora/core/agent.py](../src/agora/core/agent.py) — AgentIdentity + effective_instructions with learnings

**Fleet (orchestration)**:
- [src/agora/fleet/orchestrator.py](../src/agora/fleet/orchestrator.py) — `run_project`, `_run_phase`, per-project work_dir + git unification, `_maybe_queue_narration_redirect`
- [src/agora/fleet/agent_runtime.py](../src/agora/fleet/agent_runtime.py) — `execute_task`, shared `_run_loop`, `_emit_write_event_card`, `_compose_system_prompt`
- [src/agora/fleet/stage_runner.py](../src/agora/fleet/stage_runner.py) — micro-stage execution with fresh context per stage
- [src/agora/fleet/inner_tools.py](../src/agora/fleet/inner_tools.py) — every tool schema + factory: read/write/edit/list, fetch_url, check_python, run_pytest, git_*, mark_complete, report_learning. Also `_find_module_scope_undefined_names`, `_find_code_after_main_block`, `AUTO_HOOKED_TOOL_NAMES`.
- [src/agora/fleet/auto_hooks.py](../src/agora/fleet/auto_hooks.py) — `run_auto_hooks` + `synthesize_mark_complete`
- [src/agora/fleet/auto_learning.py](../src/agora/fleet/auto_learning.py) — `synthesize_failure_learning`
- [src/agora/fleet/runtime_postconditions.py](../src/agora/fleet/runtime_postconditions.py) — subprocess-backed postcondition factories
- [src/agora/fleet/distiller.py](../src/agora/fleet/distiller.py) — hierarchical map-reduce for large files
- [src/agora/fleet/control.py](../src/agora/fleet/control.py) — OrchestratorControl: notes, redirects, task_comments, task_card_events, task_reactions, handle_command/reaction/reply
- [src/agora/fleet/web_fetch.py](../src/agora/fleet/web_fetch.py) — SSRF-safe fetcher with single-retry on transient errors
- [src/agora/fleet/_subprocess.py](../src/agora/fleet/_subprocess.py) — env-whitelisted subprocess with timeout
- [src/agora/fleet/vram.py](../src/agora/fleet/vram.py) — VRAM pre-flight + model warm-up via Ollama /api/ps and /api/generate
- [src/agora/fleet/llm_adapter.py](../src/agora/fleet/llm_adapter.py) — Anthropic + Ollama adapters with the JSON-in-content fallback for non-tool-call models

**Observer (Matrix side)**:
- [src/agora/observe/formatters.py](../src/agora/observe/formatters.py) — every FormattedMessage factory (phase, task, review summary with ArtifactSnapshot, write-event card, command reference)
- [src/agora/observe/review.py](../src/agora/observe/review.py) — ReviewCoordinator, `_gather_artifact_snapshot` (files + git log + failures + reaction counts), `_ANSWER_TO_PHASE` with alias map
- [src/agora/observe/polls.py](../src/agora/observe/polls.py) — MSC3381 poll build/parse
- [src/agora/observe/commands.py](../src/agora/observe/commands.py) — `/agora` verb parser, including `comment`
- [src/agora/observe/renderer.py](../src/agora/observe/renderer.py) — renders phase/task events to formatted room messages
- [src/agora/observe/sync_service.py](../src/agora/observe/sync_service.py) — Matrix sync loop
- [src/agora/matrix/sync.py](../src/agora/matrix/sync.py) — EventDispatcher with reactions + reply relations

---

## How to relaunch a run (future-me quickstart)

1. **Start Ollama** (if not already): `ollama serve` in a terminal. Check
   `curl http://localhost:11434/api/tags` returns JSON.
2. **Start Conduit**: `docker-compose up -d` in the `conduit/` dir.
   Ensure the `agora.local` homeserver is reachable at `http://localhost:6167`.
3. **Activate the venv**: `.venv/Scripts/python.exe` is the interpreter to use
   (Windows layout; `source .venv/bin/activate` on POSIX).
4. **Archive the previous run**: `mv workspace/<project> workspace/<project>.runN`
   so the fresh run starts clean.
5. **Run**: `.venv/Scripts/python.exe scripts/run_<project>_test.py`.
6. **Watch Element** as `@fabs:agora.local` for phase banners, write-event
   cards with reaction hints, and the REVIEW poll.
7. **Observer commands** (type in the project room):
   - `✅`, `🔁`, `💬` reactions on a task card (informational + reply hint)
   - Reply button → plain text → becomes an implicit task comment
   - `/agora pause` · `resume` · `abort`
   - `/agora note <text>` — attach note for all agents
   - `/agora comment <task_id> <text>` — per-task feedback
   - `/agora review approve` · `reject` (== `reject_implementation`) · `retry` · `reject_analysis` · `reject_architecture` · `reject_testing`

---

## Auxiliary memory (session-crossing notes)

The following are kept in `<home>\.claude\projects\PROJECT\memory\`
and apply to future sessions:

- `project_paused_for_hardware.md` — hardware upgrade (~24 GB VRAM) planned May 2026;
  switching to `qwen2.5:14b-instruct` or `qwen2.5:32b` is the next big lever.
- `project_fetch_retry_planned.md` — hardened in Round 5 (`save_as` + bounded retry).
- `project_model_capability_ceiling.md` — qwen2.5:7b reproduces hallucinations even
  after learnings; framework fine, model is the bottleneck.

---

## Deferred / not done

Not bugs, just intentional deferrals:

- **Elements of scope creep**: build_skeleton stages tend to write extra files
  (README.md, requirements.txt) beyond their declared `output_path`. Soft-warn
  fires; tests pass anyway. Harmless but noisy. A stricter stage instruction
  could forbid this.
- **Stub functions shadowing decorated endpoints** (Run 16). AST could detect
  same-name function defs where only one has an `@app.*` decorator and warn.
- **Checkpoint + resume**: `agora resume <project_room_id>` to continue a run
  after a crash. Scaffolding exists (Matrix timeline is authoritative) but
  never tested end-to-end.
- **Task hierarchy / sub-tasks**: for multi-module projects where a task spawns
  children at runtime. Flat DAG works up to ~20 tasks; beyond that the
  renderer would need a tree view.
- **qwen2.5-coder:7b integration**: coder-tuned models emit tool calls as JSON
  in content. The JSON-in-content fallback parser exists but coder still
  produced worse results than instruct (Run 6). Not worth more time without
  a fundamentally different model.

---

## Test infrastructure — how to keep the gate

- **Full suite**: `.venv/Scripts/python.exe -m pytest tests/ --cov=agora --cov-fail-under=80 --timeout=90 -q`
- **Per-module** smoke tests while developing: `pytest tests/fleet/test_<name>.py -v`
- **Coverage gate** is 80% globally. Rebuilding it would require rewriting
  the MCP handler tests (currently 78% covered) or the VRAM module (68%) —
  both genuinely hard to unit-test without a live GPU.

462 unit/integration tests protect the framework. The e2e tests in
`tests/fleet/test_e2e_live.py` skip by default; set `AGORA_E2E=1` with
Conduit running to exercise them.

---

## Post-2026-04-17 work

The 2026-04-17 snapshot above closes the discord-bot/fastapi-crud
stress-test week with framework stable on qwen2.5:7b. The work below
covers the next nine days: a multi-provider adapter, a third test bed
(URL shortener), the plan-builder meta-flow, and four progressive
hardening rounds whose load-bearing logic now lives in `src/agora/plan/`.

**Test count drift**: 501 → ~1090. Coverage gate held; new code carries
its own tests (`tests/plan/test_structure.py` 71 tests,
`tests/fleet/test_llm_adapter_litellm.py` 34 tests, etc.).

### Multi-provider — LiteLLM adapter

[fleet/llm_adapter.py](../src/agora/fleet/llm_adapter.py) gained
`LiteLLMAdapter`, a thin shim over `litellm.completion()` that maps any
`provider/model-id` (OpenAI, Anthropic, Gemini, Mistral, Together, …)
through a single Agora-shaped `LLMAdapter` interface. The whitelist of
permitted prefixes lives in `LITELLM_PROVIDER_PREFIXES`; the harness's
`create_llm_adapter` dispatches by prefix. Ollama remains its own adapter
to keep the local-first path honest.

Cost tracking landed in the same change: `litellm.completion_cost()` is
called per response and aggregated into `AgentRuntime.token_usage` (now
holding both int token counts and float `cost_usd`).
[fleet/agent_runtime.py](../src/agora/fleet/agent_runtime.py)'s
`_merge_usage` was extended to handle the float field; `harness.py`'s
`_print_summary` displays per-run cost; `preflight_vram` skips when the
target is a non-Ollama model.

Empirical effect (URL shortener test bed): gpt-4o-mini ran the
plan-builder for ~$0.025/run and the executor for similar; gpt-4o ran
the executor for ~$0.40 per attempt. Numbers are session-memory estimates
not log-recorded — see `runs/registry.yaml` `cost.source: estimated`.

### Plan-builder meta-flow

[flows/plan-builder.plan.yaml](../flows/plan-builder.plan.yaml) is an 11-task
DAG that *builds* a v2.0 executor plan from a brief: gather context,
decide library + storage, author api_spec, draft tasks, vote, emit. It
runs through the same `run_plan.py` machinery as any other plan, so its
postconditions are first-class.

This flow is the one that drove most of the post-2026-04-17 hardening
work — the 14 archived plan-builder runs at
[workspace/plan-builder.run*/](../workspace/) are the empirical record. See
`runs/registry.yaml` for per-run breakdowns and
`runs/findings.md` for the cross-cutting analysis.

### Round-by-round evolution (14–18)

Same shape as the table for Rounds 1–13.

| Round | Problem observed | Fix added | Result |
|---|---|---|---|
| 14 | gpt-4o-mini gets stuck in `edit_file_replace` non-unique-match retry loops; error message just says "matches N places" with no anchors | `_format_match_locations` shows line numbers + 1-line context for every candidate match in the error; upsert-tool hint appended | Edit-loop killings stopped; 4o-mini ergonomics close to Claude Code |
| 15 (C1) | Plan-builder produces plans whose api_spec doesn't cover all brief deliverables | `api_spec_covers_brief_deliverables` predicate with `_BRIEF_VERB_KEYWORDS` (add/lookup/list/persist/save/…) + plan-builder postcondition gate | Brief-coverage gap caught in `define_api`; plan-builder.run4 confirmed 7B couldn't absorb the feedback even so |
| 15 (C4a/C4b) | Architect emits `output_path: plan/<x>.py` (C4a missed it) or impl tasks reference modules absent from api_spec (C4b silently dropped them) | `_validate_src_path_in_api_spec` covers any non-test .py file (not only `src/`); `_align_impl_tasks_to_spec` raises instead of dropping | Plan-builder.run10 + run11 triggered both fixes |
| 15 (C5) | api_spec.md parses but contains duplicate module headers, free-text prose, or markdown bullets that survive `ast.parse` (e.g. `- src/cli.py` parses as `-(src/cli.py)`) | `api_spec_is_valid` rejects duplicate paths, parse failures, duplicate class/function names; `_find_stray_top_level_statements` rejects any top-level node that isn't ClassDef/FunctionDef/Import/docstring | Plan-builder.run9 (bullet) + run13 (test-module) caught at gate; `strip_test_module_sections` auto-heals on write_file when 4o-mini kept reintroducing the same error |
| 16 (Structural L1/L2/L3) | Field-name typos (`url_mapping` vs `url_hash_map`) and contract-vs-test drift survive every text-level check; `python_imports` passes on broken code because nothing tests behaviour | New module [plan/structure.py](../src/agora/plan/structure.py) (~900 lines): `extract_contract` (L1), `extract_usage_traces` (L2 with parent map + alias tracking + comprehension/for-stmt handling), `extract_impl_classes` (L3); `check_usage_matches_contract` and `check_impl_self_consistent`; `Mode.PERMISSIVE` (default) vs `Mode.STRICT` | Catches contract-test drift and self-inconsistent impls before runtime; opt-in strict mode for high-confidence shipping |
| 17 (Phase 2) | Tester writes assertions against the wrong return type (declares `tuple[str,str]` but asserts `dict[str,str]`); test passes against a planted impl, fails against the real one | `_reject_return_type_drift` in `fill_test_body` cross-checks tester output against contract before accepting the stage | Run-12 of plan-builder validated; tester drift now caught at fill_test_body, not at execute time |
| 17 (Phase 3) | Impl class reads from `self.attribute_a` but only ever writes to `self.attribute_b` — typo of an instance attribute name | `class_attributes_consistent` predicate using L3 structural analysis; auto-injected via `_auto_inject_class_attrs_consistency` onto every impl task that already has `py_compiles(src/*.py)` | Field typos caught structurally; gpt-4o run on URL shortener passed this gate cleanly |
| 18 (Tool lockdown) | Architect calls `delete_file('plan/api_spec.md')` after `define_api` succeeds; calls `add_class` on `src/<x>.py` from `author_spec` stage | `hide_tools` expanded in every author_* stage to include `write_file`, `delete_file`, `edit_file_*`, `fetch_url`, `add_class*`, `add_function*` | Plan-builder.run10 was the canonical "architect deleted its own output" case; lockdown made it impossible after |

### Empirical evidence — extended Run log

Same shape as the original Run table. Full per-run records with extracted
task pass/fail and durations live in [docs/runs/registry.yaml](runs/registry.yaml);
this short summary catches the cross-tier comparison highlights:

| Run | Project | Model | First-pass green | Outcome | Notes |
|---|---|---|---|---|---|
| URL-1 | url-shortener-mvp | qwen2.5:7b | 2/10 | 62-min stuck-loop | Capability-ceiling baseline |
| URL-2 | url-shortener-mvp | gpt-4o-mini | 2/4 | killed (edit-loop) | Drove edit-tool ergonomics fix |
| URL-3..6 | url-shortener-mvp | gpt-4o-mini | 2/4 each | partial | C5 / phase-2 progressively wired |
| URL-7 | url-shortener-mvp | gpt-4o-mini | 2/4 | partial — value typo | Framework gates passed; 4o-mini failed value-level test |
| URL-live | url-shortener-mvp | gpt-4o | structural gates passed; 4/5 contract tests | partial | Tester constructed wrong tuple — model-reasoning-depth gap, not framework gap |
| PB-1, PB-2 | plan-builder | qwen2.5:7b | 1/4–1/5 | aborted | Silent failure + MSC3381 polls bug |
| PB-3 | plan-builder | qwen2.5:7b | 11/21 | structurally broken plan | Pre-Approach-C |
| PB-4 | plan-builder | qwen2.5:7b | 5/9 | C-failed | Confirmed 7B can't absorb C1 feedback |
| PB-5..13 | plan-builder | gpt-4o-mini | 5/9–11/21 | various structural defects, framework hardened in response | Each defect drove a specific gate (C5, auto-strip, C4a/b, edit-loop fix, …) |
| PB-14 | plan-builder | gpt-4o-mini | 11/21 | **REFERENCE PLAN-BUILDER GREEN RUN** | All gates active, plan came out clean, ~$0.025 |
| CR-1, CR-2 | code-review | qwen2.5:7b | 0/5 each | aborted | Reviewer agent under 7B doesn't produce usable reviews — defaults to "looks clean" on every file |

### What the framework cannot do (extended)

The 7B-only "cannot" list from the original snapshot still applies. The
multi-provider runs added two more cases:

- **Catch value-level errors that aren't structural.** gpt-4o on the URL
  shortener passed every framework gate and 4/5 tests; the failing test
  failed because the tester constructed the wrong tuple shape — a
  value-level reasoning bug the framework cannot see without running the
  tests, and that the model under test cannot see in itself. This is the
  practical ceiling for a sub-Sonnet model on this DAG.
- **Replace model judgment in code review.** The code-review flow runs
  cleanly on 7B and produces structured output, but the actual review
  *content* is "looks clean" regardless of whether the file is clean. The
  reviewer agent under 7B has effectively zero analysis capability for this
  task. The framework can't synthesize judgment that isn't there.

### Cross-tier hierarchy — observed

| Tier | Effective working memory | Agentic recommended | $/run on URL-shortener exec |
|---|---|---|---|
| qwen2.5:7b | ~4k | no | $0 (local) — but capability-bound |
| qwen2.5:14b (untested) | ~8k extrap. | maybe | $0 — pending hardware |
| gpt-4o-mini | ~8k | yes | ~$0.025 estimated |
| gpt-4o | ~16k | yes | ~$0.40 estimated |
| claude-sonnet-4-6 (untested) | ~24k extrap. | yes | n/a |
| claude-opus-4-7 (untested) | ~40k+ extrap. | yes | subscription |

The same DAG runs on each tier; the DELTA across tiers is what `runs/`
documents. Lower tiers benefit from the framework — every gate caught a
bug the model would otherwise have shipped. Higher tiers may benefit
*less*: an agent with 24k+ effective working memory can hold the whole
brief plus the contract plus its own draft in context, so postcondition
gates feel more like ceremony than scaffolding. This is an open question
not a finding — see [runs/findings.md](runs/findings.md) Section 6.

---

## Operating principles (held throughout)

These are the rules that emerged from the run history and held across
all 18 rounds:

1. **Resist adding postconditions speculatively.** Every postcondition
   added in Rounds 1–11 was driven by a failure observed in a real run.
   If a new failure shape hasn't shown up, don't pre-emptively guard
   against it — friction without evidence of value.
2. **The framework is at the asymptote for 7B.** Diminishing returns on
   more framework work against this model. The real leverage is
   upgrading to 14B+ class. Before spending another round on 7B-specific
   scaffolding, ask: would this same capability just work at 14B?
3. **Don't regress the test count or coverage gate.** Test count was
   ~501 at 82% in the original snapshot; ~1095 at 80%+ post-2026-04-17.
   A refactor that drops below the gate is a refactor that destroyed
   evidence.
4. **Confirm the framework still delivers before extending it.**
   `python scripts/run_discord_bot_test.py` should hit DONE 12/12 in
   ~6 minutes on `qwen2.5:7b-instruct`. If it regresses after a change,
   that change broke an invariant.
