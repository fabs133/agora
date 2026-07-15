# Setup — from a fresh clone to a green run

This is the one document that takes a brand-new checkout to a working local
stack and a real run. Follow it top to bottom; every command is meant to be
copy-pasted. Every external boundary is configured in exactly one place — your
`.env` — and `agora doctor` tells you the moment anything is misconfigured.

> Windows note: commands are shown for POSIX shells. On Windows use
> `.venv\Scripts\activate` and Git Bash (or adapt the `cp`/`source` lines).

## What you actually need

Agora comes in **two tiers**. The core needs **Python + Ollama and nothing
else** — no Docker, no homeserver, no accounts:

| Tier | Needs | Gets you |
|---|---|---|
| **1 — Core (this document, §1–§5)** | Python ≥3.12 · Ollama · git | The validated end-to-end run: a real project built, tested and documented by a local model, gated at every phase. Provenance in JSONL + `run.log`. |
| **2 — Live view (optional, §7)** | + Docker + Conduit | Watch it happen in Element: phase banners, per-task cards, the review poll. Nice; never required. |

**Tier 2 is genuinely optional.** A phased run posts to a Matrix room only if you
ask it to; with the observer off it prints `[SKIP] conduit` and drives the whole
lifecycle with no homeserver in existence. Verified on 2026-07-15 with Docker
entirely stopped. Provenance is unconditional either way — the JSONL records and
`run.log` are written whether or not anyone is watching.

## Hardware & what to expect

The validated run drives the `p40-24gb` cast: **gemma-e4b** (implementer +
tester) and **qwen2.5:7b-instruct** (verifier).

| | |
|---|---|
| **VRAM** | **~15 GB** to hold both cast models resident (9.6 + 4.7 GB), plus context headroom |
| **Model download** | ~14.3 GB, one time |
| **Validated on** | **Tesla P40 24 GB**, Windows 11, Ollama 0.31.1, Python 3.14.3 |
| **Runtime** | **~32 minutes** on that box (measured, P3→P9) |
| **Success looks like** | `next: done (all phases green or waived)` |

> **Smaller hardware is an open item, not a promise.** The cast is a
> 24 GB-envelope binding; there is no published smaller-hardware cast yet. You
> can lower `OLLAMA_MAX_LOADED_MODELS` to 1 and let the daemon evict between
> seats (slower — it reloads 9.6 GB on every verifier task), or write your own
> cast. Neither is validated. If you make one work, that is worth an issue.

> **The reference numbers are one sample, not a guarantee.** 32 min and
> "all green" come from a single measured run
> ([session log](runs/lifecycle-baseline/session-log.md), tag
> `lifecycle-baseline-1`). The models are **not deterministic even at a fixed
> seed** — a second run on the same box, same seed, same params drew a different
> implementation defect and stopped at a gate. See
> [A stopped run is not a broken run](#a-stopped-run-is-not-a-broken-run).

## Prerequisites

- **Python ≥ 3.12** — see the interpreter note below.
- **Ollama** — the local LLM backend (<https://ollama.com>).
- **git**
- *(Tier 2 only)* **Docker** with Compose v2, for the Conduit homeserver.

> **Check which Python you actually have.** `pip install` refuses outright below
> 3.12, and `python` is not always the interpreter you think:
>
> ```bash
> python --version          # must print 3.12 or newer
> py -0p                    # Windows: lists every installed interpreter
> ```
>
> If your default is older, name the one you want explicitly — e.g.
> `py -V:3.12 -m venv .venv` (Windows) or `python3.12 -m venv .venv`. Tested on
> **3.12** (the floor) and **3.14.3** (the lifecycle baseline ran on it).

> **WSL:** if you run the code inside WSL but Ollama on the Windows host, point
> the code at the host: set `AGORA_OLLAMA_BASE_URL=http://<windows-host-ip>:11434`
> in `.env` (the WSL `localhost` is not the host's).

---

# Tier 1 — the core path

## 1. Clone, virtualenv, install

```bash
git clone <repo-url> agora && cd agora
python -m venv .venv                 # see the interpreter note above if this isn't >=3.12
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## 2. Configure — one file, one source of truth

```bash
cp .env.example .env
```

For a Tier-1 run the only line that matters is the Ollama endpoint, and the
default is already right for a local daemon:

```bash
AGORA_OLLAMA_BASE_URL=http://localhost:11434
```

`.env` is gitignored — never commit it. Everything else in the file is either a
tunable with a sane default or a Tier-2 (Conduit) setting you can ignore for
now. **You do not need `AGORA_MATRIX_PASSWORD` for a Tier-1 run.**

## 3. Start Ollama and pull the cast's models

```bash
ollama serve &                       # if it is not already running
ollama pull gemma4:e4b               # 9.6 GB — implementer + tester
ollama pull qwen2.5:7b-instruct      # 4.7 GB — verifier
```

Both models resident at once needs ~15 GB of VRAM. If your daemon is configured
to hold only one, it still works — it just reloads between seats.

> **Record the digests you actually pulled.** A tag can be re-pushed, so "the
> same tag" is not "the same weights", and a digest mismatch is the first thing
> to check if your results diverge from the reference:
>
> ```bash
> curl -s http://localhost:11434/api/tags | python -c "
> import sys, json
> for m in json.load(sys.stdin)['models']:
>     print(f\"{m['name']:32} {m['digest'][:16]}\")"
> ```
>
> ```text
> gemma4:e4b                       c6eb396dbd5992bb     <- the baseline's digests
> qwen2.5:7b-instruct              845dbda0ea48ed74
> ```
>
> (`ollama show <model>` does **not** print a digest on current Ollama, despite
> older docs saying so — `/api/tags` is where it lives.)

## 4. Preflight — everything green before you run

```bash
agora doctor
```

One health check, shared by every entry point: Ollama reachable, the cast's
models present, VRAM headroom, workspace/git — one `[ OK ]` / `[FAIL]` line
each, a fix hint on every red, non-zero exit if anything is red. Conduit shows
as `[SKIP]` when the observer is off; a skip is **not** a green — it means the
check did not run because that dependency isn't in your path.

Do not proceed until nothing is red.

## 5. Run the lifecycle

```bash
python scripts/run_phased.py campaigns/integration-run-2.yaml --status   # P3..P9 pending
python scripts/run_phased.py campaigns/integration-run-2.yaml --auto     # go
```

`--auto` advances while green: it runs each phase in the flow's order, evaluates
that phase's gate, and continues only if the gate passes. On the reference box
this reaches `next: done` in ~32 minutes, having built **echobot** — a command
router plus a stdin/stdout adapter — from an empty directory:

```text
=== integration-run-2 — phase status ===
  P3 green | P4 green | P5 green | P6 green | P7 green | P9 green
next: done (all phases green or waived)
```

Nothing left the machine. The model wrote the code, wrote its own tests, ran
them, built a CLI adapter, and documented the result. Try what it built:

```bash
$ cd runs_out/integration-run-2/echobot/echobot
$ printf '!ping\n!roll 2d6\n!help\n' | python -m echobot
pong
rolled 2d6: 5+5=10
Available commands:
  !ping - Returns 'pong'.
  !echo <text> - Repeats the given text verbatim.
  !roll NdM - Rolls N dice with M sides (e.g., !roll 20d6).
  !help - Shows this help message.
```

`runs_out/integration-run-2/` holds the provenance: `phases.jsonl` (gate
records), `tasks.jsonl` (per-task outcomes, tool usage, retries) and `run.log`
(every turn, every tool result, every rejection).

### Prefer one phase at a time?

`--auto` is a loop around the single-phase mode; both share one execution path.

```bash
python scripts/run_phased.py campaigns/integration-run-2.yaml --next   # run exactly one phase, then stop
```

## 6. A stopped run is not a broken run

**Expect this. It is the product working, not failing.**

A phase gate stops the run when a *blocking* task misses its postconditions:

```text
=== phase P5 gate: RED ===
  blockers: T5.1
  [FAIL] T5.1 (block)
      FAIL run_check python -m pytest -q  ->  2 failed, 6 passed

[auto] STOPPED at P5 — gate RED (report above). Repairs are operator actions; --auto does not guess.
    python scripts/run_phased.py <campaign> --rerun-task <id> --oracle P5
```

The whole point of the framework is that a weak local model is **not** trusted
to self-report. Gates verify each task independently; when one fails, the run
halts rather than building on a broken foundation. A red gate is information.

The models are non-deterministic even at a fixed seed, so **different runs draw
different defects** — the reference run went straight through; the very next run
on the same box drew a bad implementation string and stopped at P5. Both are
correct behaviour.

What to do:

```bash
# 1. Read the gate report. It names the failing predicate and shows the run_check output verbatim.
# 2. Repair the task that OWNS the defect — often in an earlier phase than the red gate:
python scripts/run_phased.py campaigns/integration-run-2.yaml --rerun-task T4.1 --oracle P5
# 3. Continue:
python scripts/run_phased.py campaigns/integration-run-2.yaml --auto
```

`--oracle <phase>` hands the model the failing gate's output verbatim — a repair
can only be as good as the defect the gate can *name*. Protocol: **one repair
per red gate; a second red on the same gate stops the run for good.** That
limit is deliberate: past that point you are teaching to the test, not fixing
the artifact.

---

# Tier 2 — the live observation view (optional)

Everything above works with no homeserver. This tier only adds a **human
window** onto a run: phase banners, per-task write-event cards, and the
REVIEW-phase poll, streaming into Element as they happen.

**Skip this section entirely unless you want to watch.**

## 7. Conduit + accounts

```bash
cp conduit/conduit.example.toml conduit/conduit.toml
# edit conduit/conduit.toml: set registration_token to match
# AGORA_MATRIX_REGISTRATION_TOKEN in your .env
(cd conduit && docker compose up -d)
```

Conduit listens on `http://localhost:6167`. Register the two accounts — the
system agent (`@agora`) and the human observer (`@observer`):

```bash
# system agent — must match AGORA_MATRIX_USER_ID / AGORA_MATRIX_PASSWORD
curl -X POST http://localhost:6167/_matrix/client/v3/register \
  -H 'Content-Type: application/json' \
  -d '{"auth":{"type":"m.login.registration_token","token":"dev_only_CHANGE_ME"},"username":"agora","password":"agora-dev-pass"}'

# human observer — matches AGORA_OBSERVER_USER / AGORA_OBSERVER_PASSWORD
curl -X POST http://localhost:6167/_matrix/client/v3/register \
  -H 'Content-Type: application/json' \
  -d '{"auth":{"type":"m.login.registration_token","token":"dev_only_CHANGE_ME"},"username":"observer","password":"observer-dev-pass"}'
```

Set **`AGORA_MATRIX_PASSWORD`** in `.env` (required once the observer is on — a
run fails loudly if it is empty), then `agora doctor` should show
`[ OK ] conduit` instead of `[SKIP]`. Point Element at `http://localhost:6167`
and join the project room — see [element-setup.md](element-setup.md).

## 8. Alternate quick demo — the Discord-bot build

A shorter, single-command build that drives one 7B model through a 12-task DAG.
**Requires Tier 2** (it enables the observer and posts a REVIEW poll).

```bash
python scripts/run_discord_bot_test.py
```

**No Discord account needed, and nothing leaves your machine.** Despite the
name it *builds* a Discord bot's code and verifies it in a sandbox; it never
connects to Discord, and the `DISCORD_TOKEN` the generated code reads is a dummy.

> **Status: not verified end-to-end.** Unlike the Tier-1 lifecycle, this demo has
> no recorded run reaching its `DONE 12/12`, so there is no honest runtime or
> success figure to quote. Treat it as a demonstration, not a benchmark; the
> validated path is §5.

At the REVIEW phase it posts a poll and waits `AGORA_REVIEW_TIMEOUT_SECONDS`
(default 300) for a human vote. Vote in Element to decide immediately. If nobody
votes, it decides on its own — **approving only if every task passed**, and
otherwise looping back to rework rather than rubber-stamping.

---

## The test suite (no Ollama, no Conduit)

```bash
pytest -q                          # fast, fully self-contained
pytest --cov=agora --cov-fail-under=80 -q
```

Live end-to-end tests are gated behind `AGORA_E2E=1`.

## Developer tooling (optional)

Architecture diagrams render from `docs/architecture/*.puml` via a local
PlantUML server:

```bash
# start a PlantUML server on :18080 (override with AGORA_PLANTUML_URL), then:
python scripts/render_diagrams.py
agora doctor --dev                 # doctor + the PlantUML server check
```

`render_diagrams.py` is standalone (stdlib only) so it runs in a bare checkout.

---

## Troubleshooting

Most failure modes surface through `agora doctor` with a named fix hint:

| Symptom | Fix |
|---|---|
| `ollama: unreachable` | `ollama serve`. If it refuses to *bind*, see the port note below. |
| `ollama-models: missing …` | `ollama pull <name>` for the cast's models |
| `vram: … won't fit` | free VRAM, or hold one model at a time (`OLLAMA_MAX_LOADED_MODELS=1`) |
| `[SKIP] conduit` | not an error — the observer is off and no homeserver is needed |
| `conduit: … no password set` | Tier 2 only: set `AGORA_MATRIX_PASSWORD` in `.env` |
| `conduit: login failed` | is Conduit up (`docker compose up -d`) and the account registered? |
| `Matrix login timed out after 8s` | Tier 2 only: Conduit is down or unreachable. Start it, or turn the observer off and run Tier 1. |
| `workspace: not a git work tree` | run from inside the cloned repo |
| **A phase gate went RED** | **Not a bug** — see [§6](#6-a-stopped-run-is-not-a-broken-run) |

### `ollama serve` won't bind, but nothing is on the port

On Windows, `ollama serve` can die with *"An attempt was made to access a socket
in a way forbidden by its access permissions"* while `netstat` shows **11434
free**. Windows reserves dynamic port ranges for WinNAT/Hyper-V, and Docker
Desktop's presence can swallow 11434. Check:

```bash
netsh interface ipv4 show excludedportrange protocol=tcp
```

If a reserved range covers 11434, move the daemon and point Agora at it — one
line, no code change:

```bash
OLLAMA_HOST=127.0.0.1:11700 ollama serve
# .env:
AGORA_OLLAMA_BASE_URL=http://localhost:11700
```

### A long run looks dead

Check `runs_out/<campaign>/run.log` — it streams every turn, tool call and
rejection as they happen. A phase can legitimately spend minutes inside a single
model turn, and a 9.6 GB model load from a cold disk is not instant.

### `docker compose` warns about the `version` key

Harmless — Compose v2 ignores the obsolete `version:` attribute.

---

## Where to go next

- **[README](../README.md)** — what the framework is and the ideas it tests.
- **[docs/runs/lifecycle-baseline/session-log.md](runs/lifecycle-baseline/session-log.md)**
  — the reference run in full: conditions, every gate, per-task provenance,
  deviations and confounds. The honest version of "it works".
- **`OLLAMA.md`** — the author's own box, warts and all. It is a **worked
  example of one machine's envelope, not setup instructions**: the model
  directory, GPU UUIDs and port there are specific to that host. Read it for the
  reasoning, not the values.
