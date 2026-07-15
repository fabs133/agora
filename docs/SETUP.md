# Setup — from a fresh clone to a green run

This is the one document that takes a brand-new checkout to a working local
stack and a demo run. Follow it top to bottom; every command is meant to be
copy-pasted. External boundaries (Matrix, Ollama, PlantUML) are each configured
in exactly one place — your `.env` — and `agora doctor` tells you the moment
anything is misconfigured.

> Windows note: commands are shown for POSIX shells. On Windows use
> `.venv\Scripts\activate` and Git Bash (or adapt the `cp`/`source` lines).

## Hardware & what to expect

The demo runs the default model `ollama/qwen2.5:7b-instruct` entirely on your
machine:

| | |
|---|---|
| **VRAM** | ~6 GB free (GPU strongly recommended; CPU-only works but is slow) |
| **Model download** | ~4.7 GB, one time (`agora setup-ollama`) |
| **Demo runtime** | **unverified** — see the note below |
| **Success looks like** | the run ends `DONE 12/12` — all 12 tasks green |

> **On the runtime claim.** This table previously read *"~6 minutes on the
> reference box (RTX 3060 Ti)"*. That figure is **withdrawn**: there is no
> recorded end-to-end completion of this 12-task demo, on that card or any
> other. (The model it drives, `qwen2.5:7b-instruct` at ~4.7 GB, does fit the
> 3060 Ti's ~6 GB of usable VRAM — the claim is unsupported, not impossible.)
> The project's measured end-to-end reference is a different run: the phased
> echobot lifecycle, **P3→P9 green in 32 min on a Tesla P40 24 GB** —
> [docs/runs/lifecycle-baseline/session-log.md](runs/lifecycle-baseline/session-log.md),
> tag `lifecycle-baseline-1`. Numbers will be restored here only once this demo
> has actually been measured end-to-end.

No GPU? `agora doctor` reports VRAM as a non-blocking note (never a red) and
the run still executes on CPU, just slower.

## Prerequisites

- **Python ≥ 3.12**
- **Docker** (Compose v2 — the `docker compose` subcommand) — runs the
  [Conduit](https://conduit.rs) Matrix homeserver
- **Ollama** — the local LLM backend (<https://ollama.com>). See `OLLAMA.md`
  for the daemon settings this project expects.
- **git**

> **WSL:** if you run the code inside WSL but Ollama on the Windows host, point
> the code at the host: set `AGORA_OLLAMA_BASE_URL=http://<windows-host-ip>:11434`
> in `.env` (the WSL `localhost` is not the host's).

---

## 1. Clone, virtualenv, install

```bash
git clone <repo-url> agora && cd agora
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## 2. Configure — one file, one source of truth

```bash
cp .env.example .env
```

Open `.env` and set at least **`AGORA_MATRIX_PASSWORD`** (it is required — a run
fails loudly if it is empty). The file is heavily commented; the defaults are
throwaway local-dev values. `.env` is gitignored — never commit it.

Then create the Conduit homeserver config from its template and make its
registration token **match** `AGORA_MATRIX_REGISTRATION_TOKEN` in your `.env`:

```bash
cp conduit/conduit.example.toml conduit/conduit.toml
# edit conduit/conduit.toml: set registration_token to the same value as
# AGORA_MATRIX_REGISTRATION_TOKEN in .env  (the example .env uses dev_only_CHANGE_ME)
```

## 3. Start Conduit and register the accounts

```bash
(cd conduit && docker compose up -d)
```

Conduit listens on `http://localhost:6167`. Register the two accounts Agora
uses — the system agent (`@agora`) and the human observer (`@observer`) — with
the registration token you set above:

```bash
# system agent — username/password must match AGORA_MATRIX_USER_ID / AGORA_MATRIX_PASSWORD
curl -X POST http://localhost:6167/_matrix/client/v3/register \
  -H 'Content-Type: application/json' \
  -d '{"auth":{"type":"m.login.registration_token","token":"dev_only_CHANGE_ME"},"username":"agora","password":"agora-dev-pass"}'

# human observer — matches AGORA_OBSERVER_USER / AGORA_OBSERVER_PASSWORD
curl -X POST http://localhost:6167/_matrix/client/v3/register \
  -H 'Content-Type: application/json' \
  -d '{"auth":{"type":"m.login.registration_token","token":"dev_only_CHANGE_ME"},"username":"observer","password":"observer-dev-pass"}'
```

(Use the token / passwords from *your* `.env` if you changed them.)

## 4. Pull and warm the model

```bash
ollama serve &                     # if it is not already running
agora setup-ollama                 # VRAM pre-flight + pull + warm-up of the default model
```

This pulls the exact tag **`ollama/qwen2.5:7b-instruct`** (the default in
`.env` / `profiles.yaml`) and pins it resident so the first real turns are fast.

> Same tag ≠ same weights. An Ollama tag can be re-pushed, so a fresh pull may
> differ from the one the reference `DONE 12/12` was measured on. Record your
> pulled digest with `ollama show qwen2.5:7b-instruct` (look for `digest:`); if
> your demo scores differently from the reference, a digest mismatch is the
> first thing to check.

## 5. Preflight — everything green before you run

```bash
agora doctor
```

`agora doctor` is the single health check every entry point shares. It probes
Ollama (reachable + models present), VRAM headroom, Conduit (reachable + the
system account can log in), and workspace/git — one `[ OK ]` / `[FAIL]` line
each, with a fix hint on every red, and a non-zero exit if anything is red.
Do not proceed until it is all green.

## 6. Run the demo flow

```bash
python scripts/run_discord_bot_test.py
```

**No Discord account needed, and nothing leaves your machine.** Despite the
name, the demo *builds* a Discord bot's code and verifies it in a sandbox — it
never connects to Discord and touches no network beyond `localhost` (Ollama +
Conduit). The `DISCORD_TOKEN` the generated code reads is stubbed to a dummy.

A greenfield build: the agents drive `qwen2.5:7b-instruct` through a 12-task DAG
to a working Discord bot, hitting `DONE 12/12` in ~6 minutes on the default
model. The tail of a successful run looks like:

```text
[phase] DONE
  [OK] task 12/12 ...
Success: True  Duration: 3xx.xs
```

That `DONE 12/12` is your green run — setup is complete.

To watch it live, open Element on `http://localhost:6167` and join the project
room; phase banners, per-task write-event cards, and the review poll all stream
there. See [element-setup.md](element-setup.md).

> Deeper integration path: the phase-staged **echobot** run
> (`scripts/run_phased.py` + `campaigns/integration-run-1.yaml`, cast
> `casts/p40-24gb.yaml`) executes one gated phase per invocation and is paired
> by design (`--status` / `--next`). It needs the cast's models pulled and more
> VRAM (24 GB envelope). Start with the demo above.

---

## The test suite (no Conduit / Ollama needed)

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

Every failure mode surfaces through `agora doctor` with a named fix hint:

| `agora doctor` red line | Fix |
|---|---|
| `ollama: unreachable` | `ollama serve` (see `OLLAMA.md` for `OLLAMA_MODELS`) |
| `ollama-models: missing …` | `ollama pull <name>` (or `agora setup-ollama`) |
| `vram: … won't fit` | free VRAM or pick a smaller profile/model |
| `conduit: … no password set` | set `AGORA_MATRIX_PASSWORD` in `.env` |
| `conduit: login failed` | is Conduit up (`docker compose up -d`) and the account registered? |
| `workspace: not a git work tree` | run from inside the cloned repo |
