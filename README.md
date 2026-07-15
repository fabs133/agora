# Agora

[![CI](https://github.com/fabs133/agora/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/fabs133/agora/actions/workflows/ci.yml)
[![Docs](https://github.com/fabs133/agora/actions/workflows/docs.yml/badge.svg?branch=main)](https://fabs133.github.io/agora/)

**[API reference & docs →](https://fabs133.github.io/agora/)**

**Agora drives a weak or cheap LLM through a DAG of specified tasks to produce
working code end-to-end.** The framework absorbs everything the model is
unreliable at — postcondition gates verify each task instead of trusting the
model's self-report, auto-hooks run the validators the model would forget,
edit primitives prevent transcription bugs, auto-learnings inject failure
traces back into the prompt on retry. The bet: with the right scaffolding,
a 7B local model can ship working code that a naked 7B model cannot.

Built on the [Manifold](https://github.com/fabs133/manifold) Specification
Pattern, with Matrix as the human-observer surface and Git as the artifact
store.

**Runs locally** on Python ≥3.12 + Ollama — no Docker, no homeserver, no
accounts; the demo wants ~6 GB of free VRAM (CPU works, slower). Docker buys
you one optional extra: the live Matrix view in
[SETUP §7](docs/SETUP.md#7-conduit--accounts), which the full lifecycle runs
without. New here? → **[docs/SETUP.md](docs/SETUP.md)**
takes you from clone to a green run. Found a rough edge the docs don't
anticipate? That's a real signal — please [open an issue](../../issues).

## Status

Active research code. Five load-bearing ideas, hardened empirically across the
axis-1 characterisation campaigns and the echobot integration runs — the ledger
of what was actually run is
[arc Appendix B](docs/arc/arc.md#appendix-b--runs-ledger-summary), and the
findings it produced are indexed in
[Appendix A](docs/arc/arc.md#appendix-a--integration-findings-index-f1f25).

The test suite and its 80% coverage floor are enforced on every push — the CI
badge above is the live answer, so this line does not carry a count that would
start rotting the moment it was written.

**Every cited run was performed on a single machine** — Windows 11 + Ollama +
a **Tesla P40 24 GB**, by the primary author. (That box's earlier work, through
the Round-18 stress-test era, ran on its RTX 3060 Ti 8 GB before the P40 was
added in May 2026.) No second-machine or non-Windows reproduction has been
verified. The framework is plain Python with standard deps, so it *should* run
anywhere Python ≥3.12 and Ollama do — but if you hit setup friction the
quickstart doesn't anticipate, that's a real signal, please open an issue.

The 24 GB card is in place; the reference lifecycle run
([session log](docs/runs/lifecycle-baseline/session-log.md), tag
`lifecycle-baseline-1`) is measured on it — see
[Known limitations](#known-limitations) for what remains unverified.

## Evidence

Three test-bed projects, four model tiers, 46 archived runs in
[docs/runs/](docs/runs/). Reference green runs:

| Run | Project | Model | Result |
|-----|---------|-------|--------|
| [discord-bot run 13](docs/runs/discord-bot.run13.md) | Discord bot, 12-task DAG | qwen2.5:7b-instruct (local) | 12/12 first-pass, 5.3 min |
| fastapi-crud run 16 | FastAPI CRUD, 13-task DAG | qwen2.5:7b-instruct (local) | 13/13 with passing pytest, 6 min, 1 loopback |
| discord-bot-full run 17 | Multi-module Discord bot, 23 tasks | qwen2.5:7b-instruct (local) | 23/23 first-pass, 10 min, 4/4 pytest green |
| [plan-builder run 14](docs/runs/plan-builder.run14-4omini-clean.md) | Plan-builder meta-flow | gpt-4o-mini via LiteLLM | structurally clean plan, ~$0.025 |

Cross-cutting analysis: [docs/runs/findings.md](docs/runs/findings.md).
Round-by-round evolution and design rationale:
[docs/lessons-learned.md](docs/lessons-learned.md).

**Integration program (echobot lifecycle).** A phase-gated program then drove a
single 9.6 GB local model (gemma) through a full software lifecycle on the
Manifold pattern: greenfield build → machine-consumable handoff → phase-0
re-validation (red-teamed) → brownfield extension via brief-as-index navigation →
re-handoff. The two shipped baselines are tagged **`echobot-v1`** (greenfield,
PROJECT_STATE.md v1.1) and **`echobot-v2`** (brownfield `!flip`/`!choose` + a
transport-injected Discord adapter, PROJECT_STATE.md v2.1). Every claim carries a
pointer into the committed record: the pre-registrations and findings F1–F25 live
in [docs/runs/integration-run-1/findings.md](docs/runs/integration-run-1/findings.md)
(Parts 1–16), and the narrative index is
[docs/arc/arc-outline.md](docs/arc/arc-outline.md).

## How it works

Five load-bearing ideas, each driven by an observed failure mode:

1. **Postconditions are ground truth, not the LLM's self-report.** Every task
   carries a `Specification` with predicates evaluated after the model is
   done. `mark_complete` is observed but doesn't determine outcome.
2. **Auto-hooks run validators silently.** After any `write_file` or
   `edit_file_*`, the framework runs `check_python`, `run_python_import`,
   `git_commit` — and validation errors land in the model's next-turn
   message history without the model needing to remember to ask.
3. **Auto-learnings feed the loopback.** When a postcondition fails, the
   framework synthesises a `Learning` from the failure tuple and injects
   it into the agent's system prompt for the retry. Same failure twice
   doesn't duplicate, it reinforces.
4. **Edit primitives instead of full rewrites.** `edit_file_replace`,
   `edit_file_insert_before`, `edit_file_append` — the model never
   re-emits whole files, so transcription bugs (`discord.InterACTION`,
   duplicated imports) effectively disappear.
5. **Staged tasks + literal templates for hard problems.** Hard tasks
   split into `Stage` objects with small iteration caps and pre-loaded
   context, fresh message history per stage. Some stages provide a
   literal "write EXACTLY this content" template — leaving no room for
   hallucination.

The failure-driven rationale and the run that surfaced each is in
[docs/lessons-learned.md](docs/lessons-learned.md).

## Quickstart

**Full walkthrough: [docs/SETUP.md](docs/SETUP.md)** — one document, clone to a
green run, with a troubleshooting table keyed to `agora doctor`.

Prerequisites: Python ≥3.12 and Ollama (the local backend). Docker is optional
— it serves only the Conduit homeserver behind the live view.

```bash
# 1. Clone, venv, install
git clone <repo-url> agora && cd agora
python -m venv .venv
source .venv/bin/activate            # POSIX  (Windows: .venv\Scripts\activate)
pip install -e ".[dev]"

# 2. Configure — one source of truth
cp .env.example .env                 # the Ollama endpoint default is already right

# 3. Start Ollama and pull the cast's models
ollama serve &
ollama pull gemma4:e4b               # 9.6 GB — implementer + tester
ollama pull qwen2.5:7b-instruct      # 4.7 GB — verifier

# 4. Preflight — everything green before you run
agora doctor                         # Ollama / VRAM / workspace; non-zero on red

# 5. Run the lifecycle
python scripts/run_phased.py campaigns/integration-run-2.yaml --auto
```

`--auto` advances phase by phase while each gate stays green, building
**echobot** from an empty directory. On the reference box that reaches
`next: done` in ~32 minutes ([session log](docs/runs/lifecycle-baseline/session-log.md),
tag `lifecycle-baseline-1`). Provenance lands in `runs_out/integration-run-2/`.

**A stopped run is not a broken run** — a red gate means a postcondition caught
something, which is the framework working. [SETUP §6](docs/SETUP.md#6-a-stopped-run-is-not-a-broken-run)
covers the repair loop.

To *watch* a run live, add the optional Conduit homeserver
([SETUP §7](docs/SETUP.md#7-conduit--accounts)) and log into Element
([docs/element-setup.md](docs/element-setup.md)) — phase banners, per-task
write-event cards and the review poll stream there. The lifecycle above runs
identically without it.

The test suite is self-contained (no Conduit / Ollama needed):

```bash
pytest tests/ --cov=agora --cov-fail-under=80 -q
```

End-to-end live tests are gated behind `AGORA_E2E=1`.

## LLM backends

Agora is model-agnostic. Pick a backend through `profiles.yaml` (preferred)
or by setting `AGORA_LLM_MODEL` for a one-off override.

### Profiles (preferred)

`profiles.yaml` at the repo root bundles `model`, `num_ctx`, `max_tokens`,
`keep_alive`, `timeout_seconds`, plus Ollama/VRAM sub-sections into named,
self-contained run profiles. One run = one profile; nothing else needs to
be set.

```bash
# Use the default profile from profiles.yaml.
python scripts/run_discord_bot_test.py

# Pick a specific profile.
AGORA_PROFILE=qwen-coder-14b-bigctx-p40 python scripts/run_discord_bot_test.py

# Point at a non-default file (e.g. per-environment).
AGORA_PROFILES_FILE=./profiles.staging.yaml python scripts/run_discord_bot_test.py
```

Per-field env overrides layer on top of the selected profile (env >
profile > schema default). The full list:

| Env var | Overrides profile field |
|---------|--------------------------|
| `AGORA_LLM_MODEL` | `model` |
| `AGORA_LLM_NUM_CTX` | `num_ctx` (use `""`/`none`/`null`/`0` to defer to Ollama's default) |
| `AGORA_LLM_MAX_TOKENS` | `max_tokens` |
| `AGORA_LLM_TIMEOUT_SECONDS` | `timeout_seconds` |
| `AGORA_OLLAMA_BASE_URL` | `ollama.base_url` |
| `AGORA_OLLAMA_NUM_PARALLEL` | `ollama.num_parallel` |
| `AGORA_OLLAMA_KEEP_ALIVE` | `keep_alive` |
| `AGORA_VRAM_SAFETY_MARGIN_MIB` | `vram.safety_margin_mib` |

A typo'd key in `profiles.yaml` raises a loud validation error rather
than silently no-opping. With no `profiles.yaml` on disk, a packaged
default reproduces the historical `ollama/qwen2.5:7b-instruct` setup,
so fresh clones + legacy `AGORA_LLM_MODEL=…` runs still work unchanged.

### Ollama (default, no API key)

```bash
ollama serve &
agora setup-ollama        # pulls and warms the configured default model
```

The default is `ollama/qwen2.5:7b-instruct` — the model used for every
empirical reference run above. Override with `AGORA_LLM_MODEL=ollama/<name>`.
The coder variants (`qwen2.5-coder:*`) tool-call less reliably at 7B
([Run 6 in lessons-learned](docs/lessons-learned.md)) — prefer instruct at
the 7B tier and coder at 14B+.

The VRAM pre-flight refuses to load a model that won't fit instead of
letting Ollama thrash into CPU offload. Skip with
`AGORA_SKIP_VRAM_CHECK=1`.

| Free VRAM | Recommended |
|-----------|-------------|
| ≥ 24 GB | `ollama/qwen2.5-coder:32b` |
| ≥ 10 GB | `ollama/qwen2.5-coder:14b` |
| ≥ 6 GB  | **`ollama/qwen2.5:7b-instruct`** *(default — empirically validated)* |
| ≥ 4 GB  | `ollama/qwen2.5:3b-instruct` |
| < 4 GB  | `ollama/qwen2.5:3b-instruct` is the practical floor (smaller models tool-call unreliably) |

**Ollama is the only backend.** The former multi-provider adapters (LiteLLM,
the Anthropic API, and the Claude Code subprocess) were removed — the model
factory is now a single live seam (`create_llm_adapter` → `ollama/<name>`),
not a menu of untested paths. A new backend re-enters through the bench
pipeline *with evidence*, not as kept dead code. Per-role model overrides
(`AgentConfig.model`) still work across Ollama model tiers.

## Matrix observer

Every run streams phase banners, per-task cards, write-event cards, and a
review poll into a project Matrix room.

- Reactions on a task card (✅ / 🔁 / 💬) — informational + reply hint.
- Threaded reply on a card — becomes an implicit per-task comment routed
  back to the agent.
- `/agora` verbs in the room: `pause`, `resume`, `abort`, `note <text>`,
  `comment <task_id> <text>`, `review approve|reject|retry`.

See [docs/element-setup.md](docs/element-setup.md) for client setup. The
observer is the primary human-in-the-loop surface; `enable_observer=False`
on the orchestrator runs Agora headless.

## Use as an MCP server

```bash
agora mcp
```

Runs the outer MCP server over stdio so any MCP-aware client (Claude
Desktop, Cursor, a custom agent) can drive Agora's verbs through the
standard protocol. See [src/agora/mcp/handlers.py](src/agora/mcp/handlers.py)
for the schema.

## Tuning knobs

Model and inference parameters live in [profiles.yaml](profiles.yaml) — see
the LLM backends section above for the full override table. The env vars
below cover the rest of the runtime (Matrix, parallelism, observer
timeouts); see [src/agora/config.py](src/agora/config.py) for the full
list.

On multi-GPU systems, the VRAM pre-flight queries the device Ollama is
using (resolved via `/api/ps`, falling back to `CUDA_VISIBLE_DEVICES`)
rather than the card with the least free memory. To force-skip the check
during debugging, set `AGORA_SKIP_VRAM_CHECK=1`.

| Env var | Default | Purpose |
|---------|---------|---------|
| `AGORA_PROFILE` | (`default:` from profiles.yaml) | Pick a named profile |
| `AGORA_PROFILES_FILE` | `./profiles.yaml` | Point at an alternate profile file |
| `AGORA_MAX_PARALLEL_AGENTS` | `3` | Concurrent task executions per phase |
| `AGORA_SKIP_VRAM_CHECK` | `false` | Force-skip the VRAM pre-flight (honored: `1`/`true`/`yes`/`on`) |
| `AGORA_REVIEW_TIMEOUT_SECONDS` | `300` | How long the REVIEW poll waits for a human vote. On timeout it is **task-aware**: approves only if every task passed, else loops back to rework — it does not blanket-approve. |
| `AGORA_MAX_TASK_RETRIES` | (per runner) | In-phase auto-retries per failed task |

Every LLM call is `async`; within a phase, ready tasks run concurrently
up to `AGORA_MAX_PARALLEL_AGENTS`.

## Going deeper

- [docs/lessons-learned.md](docs/lessons-learned.md) — project log:
  architecture diagram, the 5 load-bearing ideas with citations, Round 1–18
  evolution table, file map, failure taxonomy.
- [docs/runs/](docs/runs/) — run history archive: every run catalogued in
  [registry.yaml](docs/runs/registry.yaml), narrative deep-dives, cross-cutting
  findings, publishable thread candidates. Summary ledger:
  [arc Appendix B](docs/arc/arc.md#appendix-b--runs-ledger-summary).
- [docs/runs/findings.md](docs/runs/findings.md) — what worked, what didn't,
  model-tier comparison.
- [docs/runs/publishable.md](docs/runs/publishable.md) — three paper-shaped
  research threads with thesis, evidence, and target venue.
- [docs/runs/lifecycle-baseline/session-log.md](docs/runs/lifecycle-baseline/session-log.md)
  — the reference end-to-end run: the full echobot lifecycle (P3→P9) green in a
  single session with zero repairs, with per-task provenance and the live bot
  transcript. Tag `lifecycle-baseline-1`.
- [agora-capability-exchange](https://github.com/fabs133/agora-capability-exchange)
  — the community half: a shared, keyed record of which local models can
  actually do what, on whose hardware. **Evidence in, scores derived** — a
  contribution is raw run records, and CI re-derives the capability vector and
  rejects any row that doesn't reproduce. *Not open for contributions yet:* the
  re-derivation trust gate activates with agora `v0.2.0`, and the marketplace
  does not open before its trust gate does.

### A note on commit hashes

This repository's history was **rewritten in 2026-07** (a `git filter-repo`
secret scrub: the tracked `workspace/` run archive was removed and absolute
author paths relativized). The rewrite changed **every commit hash**. Any hash
cited in a document authored before that date — session logs, findings parts,
older design notes — **will not resolve**. Those historical documents are
deliberately left as written rather than retroactively edited.

- **Tags survived and are the durable anchors**: `echobot-v1` (`957be3f4`),
  `echobot-v2` (`15edd7c9`), `lifecycle-baseline-1`.
- **To resolve an old hash**: `grep ^<old-hash> docs/history/commit-map.txt`
  ([commit-map.txt](docs/history/commit-map.txt), 133 commits mapped).

## Known limitations

Deferred deliberately, not bugs:

- **Single-machine validation.** Every cited run, every screenshot,
  every reproduced quickstart was on one Windows 11 + **Tesla P40 24 GB** +
  Ollama setup, by the primary author (that box's pre-May-2026 work used
  its RTX 3060 Ti 8 GB). Cross-platform reproduction (Linux, macOS,
  different GPUs, different Ollama versions) is unverified — and the
  validated cast needs ~15 GB of VRAM, so a smaller-hardware cast is an
  open item, not a promise. The runner scripts include
  Windows-specific guards (UTF-8 stdout wrapping in
  [scripts/run_fastapi_crud_test.py:23-25](scripts/run_fastapi_crud_test.py#L23-L25))
  and the lessons-learned doc occasionally references Windows venv
  paths verbatim — a POSIX user should swap `.venv/Scripts/python.exe`
  for `.venv/bin/python` mentally.
- **14B+ runs are thin.** The 24 GB P40 lifted the old VRAM gate — a
  32B-class run exists ([baseline-32b.run1](docs/runs/baseline-32b.run1.md))
  and the validated lifecycle runs gemma-e4b + qwen2.5:7b-instruct — but no
  14B/32B campaign has been driven to the same standard as the 7B work.
- **Code-review flow** runs cleanly on 7B but the reviewer produces
  "looks clean" regardless of input. Framework is fine; the model has
  effectively zero analysis capability for this task at 7B. Pending
  capability uplift.
- **Checkpoint + resume.** Matrix timeline is authoritative for project
  state; `agora resume <room_id>` scaffolding exists but is not
  end-to-end tested.
- **No CI.** Local `pytest` is the gate; GitHub Actions setup is on the
  roadmap.
- **Cost values in the run archive are estimates**, not log-recorded.
  Each entry's `cost.source` field is `recorded | estimated | unknown`.
  Upgrading to recorded values needs the LiteLLM cost-tracker output to
  land in log lines — see [docs/runs/findings.md §6.4](docs/runs/findings.md).
- **Task hierarchy / sub-tasks.** Flat DAG works to ~25 tasks; multi-module
  projects beyond that would need a tree view in the renderer.

## License

See [LICENSE](LICENSE).
