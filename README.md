# Agora

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

## Status

Active research code; framework stable at Round 18 of empirical hardening.
Five load-bearing ideas validated across 46 runs on three test-bed projects
and four model tiers. Test suite: **1095 tests, 80%+ coverage**.

**All 46 cited runs were performed on a single machine** — Windows 11 +
Conduit + Ollama + RTX 3060 Ti, by the primary author. No second-machine
or non-Windows reproduction has been verified. The framework is plain
Python with standard deps, so it *should* run anywhere Python ≥3.12,
Docker, and Ollama do — but if you hit setup friction the quickstart
doesn't anticipate, that's a real signal, please open an issue.

Primary author is paused on local-hardware experiments pending a 24 GB VRAM
upgrade — see [Known limitations](#known-limitations).

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

Prerequisites: Python ≥3.12, Docker (for Conduit homeserver), Ollama
(for the default local backend).

```bash
# 1. Clone, venv, install
git clone <repo-url> agora && cd agora
python -m venv .venv
source .venv/bin/activate            # POSIX
# .venv\Scripts\activate             # Windows
pip install -e ".[dev,litellm]"

# 2. Configure the Matrix homeserver (one-time)
cp conduit/conduit.example.toml conduit/conduit.toml
# Edit registration_token in conduit/conduit.toml before exposing the port.

# 3. Start Conduit
(cd conduit && docker compose up -d)

# 4. Pull and warm the default Ollama model
ollama serve &
agora setup-ollama                   # VRAM pre-flight + pull + warm-up

# 5. Health check
agora doctor                         # probes Ollama / Conduit / GPU / claude CLI

# 6. Run a reference project
python scripts/run_discord_bot_test.py
# Hits DONE 12/12 in ~6 minutes on the default model.
```

To watch a run live, log into Element ([docs/element-setup.md](docs/element-setup.md))
on `http://localhost:6167` and join the project room — phase banners, per-task
write-event cards, and the review poll all stream there.

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
([Run 6 in lessons-learned](docs/lessons-learned.md)) — `agora doctor`
recommends instruct for the 7B tier and coder for 14B+.

The VRAM pre-flight refuses to load a model that won't fit instead of
letting Ollama thrash into CPU offload. Skip with
`AGORA_SKIP_VRAM_CHECK=1`.

| Free VRAM | Recommended |
|-----------|-------------|
| ≥ 24 GB | `ollama/qwen2.5-coder:32b` |
| ≥ 10 GB | `ollama/qwen2.5-coder:14b` |
| ≥ 6 GB  | **`ollama/qwen2.5:7b-instruct`** *(default — empirically validated)* |
| ≥ 4 GB  | `ollama/qwen2.5:3b-instruct` |
| < 4 GB  | Use the LiteLLM path with a hosted provider |

### Multi-provider via LiteLLM

```bash
pip install -e ".[dev,litellm]"
export OPENAI_API_KEY=...
export AGORA_LLM_MODEL=openai/gpt-4o-mini
python scripts/run_discord_bot_test.py
```

Routes any `provider/model-id` through a single normalised interface —
OpenAI, Anthropic, Gemini, Mistral, Together, Bedrock, and others. Cost
tracking is automatic via `litellm.completion_cost()`. Empirical numbers
from the URL-shortener test bed: gpt-4o-mini ran the executor for
~$0.025/run; gpt-4o for ~$0.40/run (estimated, not log-recorded — see
[docs/runs/findings.md §6.4](docs/runs/findings.md)).

### Anthropic API directly

```bash
pip install -e ".[dev,llm]"
export ANTHROPIC_API_KEY=sk-ant-...
export AGORA_LLM_MODEL=anthropic/claude-haiku-4-5
agora mcp
```

The dedicated `anthropic/*` adapter exists alongside the LiteLLM path and
is preferred for tool-use reliability when you only need Anthropic.

### Claude Code subprocess *(experimental, ToS-grey, opt-in)*

```bash
export AGORA_ALLOW_CLAUDE_SUBPROCESS=1
export AGORA_LLM_MODEL=claude-code/subscription
agora mcp
```

Uses the local `claude` CLI's subscription session. **Anthropic explicitly
discourages third-party products that drive claude.ai login**; this adapter
is a pragmatic workaround, not a sanctioned integration. Tool calls are
simulated by prompting for strict JSON, not native `tool_use` blocks —
reliability is lower than the API or Ollama paths. Subprocess startup
overhead also dominates short turns. Provided for users who want to
experiment without an API key.

You can mix backends per agent role (architect on the API, implementer on
Ollama) via `AgentConfig.model`.

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

| Env var | Default | Purpose |
|---------|---------|---------|
| `AGORA_PROFILE` | (`default:` from profiles.yaml) | Pick a named profile |
| `AGORA_PROFILES_FILE` | `./profiles.yaml` | Point at an alternate profile file |
| `AGORA_MAX_PARALLEL_AGENTS` | `3` | Concurrent task executions per phase |
| `AGORA_SKIP_VRAM_CHECK` | `false` | Disable VRAM pre-flight |
| `AGORA_ALLOW_CLAUDE_SUBPROCESS` | `false` | Enable `claude-code/*` adapter |
| `AGORA_CLAUDE_CODE_TIMEOUT_SECONDS` | `300` | Subprocess call timeout |
| `AGORA_REVIEW_TIMEOUT_SECONDS` | `86400` | Auto-approve after N seconds |
| `AGORA_MAX_TASK_RETRIES` | (per runner) | In-phase auto-retries per failed task |

Every LLM call is `async`; within a phase, ready tasks run concurrently
up to `AGORA_MAX_PARALLEL_AGENTS`.

## Going deeper

- [docs/lessons-learned.md](docs/lessons-learned.md) — project log:
  architecture diagram, the 5 load-bearing ideas with citations, Round 1–18
  evolution table, file map, failure taxonomy.
- [docs/runs/](docs/runs/) — run history archive: 46 runs catalogued in
  [registry.yaml](docs/runs/registry.yaml), 5 narrative deep-dives, cross-cutting
  findings, publishable thread candidates.
- [docs/runs/findings.md](docs/runs/findings.md) — what worked, what didn't,
  model-tier comparison.
- [docs/runs/publishable.md](docs/runs/publishable.md) — three paper-shaped
  research threads with thesis, evidence, and target venue.

## Known limitations

Deferred deliberately, not bugs:

- **Single-machine validation.** Every cited run, every screenshot,
  every reproduced quickstart was on one Windows 11 + RTX 3060 Ti +
  Conduit + Ollama setup, by the primary author. Cross-platform
  reproduction (Linux, macOS, different GPUs, different Conduit /
  Ollama versions) is unverified. The runner scripts include
  Windows-specific guards (UTF-8 stdout wrapping in
  [scripts/run_fastapi_crud_test.py:23-25](scripts/run_fastapi_crud_test.py#L23-L25))
  and the lessons-learned doc occasionally references Windows venv
  paths verbatim — a POSIX user should swap `.venv/Scripts/python.exe`
  for `.venv/bin/python` mentally.
- **Hardware-gated 14B+ runs.** All published reference runs are on
  qwen2.5:7b. The "next big lever" is upgrading to a 14B / 32B class
  model — pending a 24 GB VRAM card.
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
