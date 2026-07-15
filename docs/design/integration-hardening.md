# Integration hardening — plan (no code; handover to executor)

*Drafted 2026-07-09 from the integration survey. Goal: a stranger clones
the repo, follows one document, and runs a test flow — with every
external boundary configured in exactly one place. Branch:
chore/integration-hardening off main. Stages are ordered so surface
shrinks before it is unified, and the document is written last, about
the end state.*

## Scope rulings (decided)

- REMOVE: Anthropic adapter, LiteLLM gateway routing, Claude Code
  subprocess adapter — code, Settings fields, env knobs, pyproject
  optional-extras, and their tests. The adapter FACTORY SEAM stays
  (one interface, one implementation: Ollama) — future backends arrive
  via the bench pipeline with evidence, not via kept dead code.
  Historical documents under docs/runs/ are the record and are NOT
  rewritten; removals touch code, config, and setup-facing docs only.
- KEEP, demote: PlantUML — contributor tooling; leaves the user setup
  path; documented under development docs only.
- AUDIT, then default: web_fetch — if no current flow uses it,
  enable_web_fetch defaults to false; tool retained.
- OUT OF SCOPE, explicitly: making Matrix/Conduit optional. It is the
  observer surface and load-bearing for orchestrated runs; making it
  optional is an architecture change, not setup hardening. Revisit
  trigger: first external-user feedback naming Conduit as the blocking
  friction. For v1 public: one compose file, one doc section, covered
  by preflight.

## Stage 0 — verify the survey (read-only)

Greps, reported as a table before any surgery: (a) every fetch_url /
web_fetch reference in flows, tools registration, and runs provenance;
(b) every os.getenv("AGORA_...") outside config.py and every hardcoded
localhost URL (the survey names run_phased.py:738 plus three scripts —
confirm the list is complete); (c) every secret-shaped literal in src/
and scripts/ (dev passwords, tokens, @fabs observer id).
Acceptance: the three tables in the PR description; survey confirmed or
corrected.

## Stage 1 — remove the dead adapters

Delete the three implementations + their Settings fields
(anthropic_api_key, allow_claude_subprocess, claude_code_binary,
litellm prefix routing) + extras + tests. Factory reduces to the seam
plus OllamaAdapter.
Acceptance: `grep -ri "anthropic\|litellm\|claude" src/ tests/
pyproject.toml` returns nothing (case-insensitive, excluding docs/);
suite green; no orphaned config keys.

## Stage 2 — config unification (the #1 fix)

Single source of truth: the existing pydantic Settings (AGORA_* env +
.env). Every script under scripts/ consumes it; the hardcoded Ollama
URL in run_phased.py dies; the three direct-os.getenv scripts route
through Settings. Precedence, explicit and tested:
CLI flag > env var > .env > coded default — and every entry point LOGS
its effective endpoint set at startup. This is F19 generalized: an
inert or forked parameter surface silently falsifies runs; precedence
must be wired, explicit, and printed.
Acceptance: each endpoint defined in exactly one file; a test that
manipulates env and asserts scripts resolve identically to Settings();
`grep os.getenv("AGORA_` outside config.py returns nothing; no
localhost literal outside config defaults.

## Stage 3 — secrets and defaults hygiene

No secret-shaped literals in src/ or scripts/. Dev values move to a
committed `.env.example` (clearly marked LOCAL DEV ONLY): Matrix
registration token, user passwords, observer id (neutral default
@observer:agora.local — the personal id leaves the code). Required
secrets absent at runtime fail LOUDLY with the exact variable name and
a pointer to .env.example. conduit.toml gains the same template
treatment if it embeds any.
Acceptance: secret-grep of src/+scripts/ clean; fresh clone +
`cp .env.example .env` produces a working local stack.

## Stage 4 — one preflight: `agora doctor`

One module, one command, called by every entry point (kills the
per-script health reimplementations). Checks, each with a one-line
red/green verdict and a fix hint: Ollama reachable (/api/version) +
the ACTIVE CAST's models present (/api/tags vs casts/*.yaml) + VRAM
headroom (existing vram.py); Conduit reachable + login works;
workspace/git sanity. `--dev` adds PlantUML. Non-zero exit on any red.
Acceptance: killing each service in turn produces exactly its red line
and exit code; run_phased and the CLI both call this module and contain
no private health logic.

## Stage 5 — the setup document, written last

docs/SETUP.md: prerequisites (Python version, Docker, Ollama native),
then the five-step path — clone -> `cp .env.example .env` ->
`docker compose up -d` (Conduit) -> `ollama pull <models from the
cast>` -> `agora doctor` -> run the documented demo flow (the existing
echobot greenfield flow into a scratch workspace; document existing
commands, build no new demo machinery). PlantUML + render_diagrams
move to a development section. README links SETUP.md.

## Stage 6 — the gate: fresh-clone red-team

The setup doc is trusted the way phase-0 is trusted: only after it has
been seen surviving contact. Execute the doc VERBATIM in a clean
environment (fresh venv, empty dir, no inherited env vars; ideally the
second machine). Every deviation — missing step, wrong name, implicit
knowledge — is a doc bug, filed and fixed, and the walkthrough repeats
until it completes clean.
Acceptance: one uninterrupted doc-verbatim walkthrough from clone to a
green demo run. That walkthrough IS the release criterion for "taking
the project online."

## Risks / tradeoffs (stated)

- Config unification touches every script: regression risk is real;
  mitigation is the effective-config logging (drift becomes visible)
  plus the Stage-2 resolution test.
- Keeping the adapter seam vs deleting to a bare class: seam kept —
  it is one small interface already paid for, and the bench pipeline's
  whole point is re-adding backends with evidence.
- Conduit remains required: the largest single onboarding cost,
  consciously accepted for v1; revisit trigger documented above.

---

## Stage 2 clarifications (owner ruling, 2026-07-09)

**Model: one source, injected sinks.** Environment is read ONLY in
config.py (Settings). Modules receive typed values/config objects via
constructors and parameters, wired at composition roots (cli entry
points, run_phased, campaign runner, MCP server startup, test
fixtures). Library code imports neither os.environ nor Settings — the
distributor is enforceable or it is decoration.

**Q2 — full scope, two tranches.** Tranche A: endpoint / secret /
identity / config-path cluster (incl. AGORA_PROFILES_FILE) — first
commit, unblocks setup. Tranche B: behavioral knobs — HarnessConfig
fields become a nested HarnessSettings; harness.py goes env-free and
RECEIVES the built object. The campaign env-emission channel's
semantics are preserved as an explicit precedence at the composition
root: coded default < .env < process env < CLI < CAMPAIGN PARAMS
(campaign wins — pre-registered experiment conditions, F19 doctrine);
a conflicting env/CLI value logs a loud warning in the effective-set
line, never silently overrides. If tranche B exceeds a contained
refactor: stop and flag.

**Q3 — constructor defaults die.** Config-shaped parameters (endpoints,
paths, credentials) become required; the ONLY home for a default is
the Settings field. Algorithmic parameter defaults are out of scope.

**Amended acceptance for Stage 2 (end-state, after both tranches):**
(a) grep os.getenv("AGORA_ outside config.py -> nothing;
(b) grep localhost outside config.py defaults + .env.example ->
nothing (docs excluded);
(c) grep "from agora.config import" outside the composition-root
allowlist (cli.py, scripts/ entrypoints, server.py, tests fixtures)
-> nothing;
(d) precedence test: campaign param beats env beats .env beats coded
default, and the conflict-warning line appears when env fights a
campaign knob;
(e) the resolution test from the original plan (scripts and Settings
cannot disagree under env manipulation).

---

## Stage 2B rulings (owner, 2026-07-09 — response to the stop-and-flag)

1. **Harness shape: FLAT** `harness_*` fields on Settings, existing
   env names preserved verbatim (campaign emission is the binding
   constraint). Grouping via ONE builder — `harness_config()` — the
   only Settings->HarnessConfig mapping point in the codebase.
2. **Q3: STRICT-REQUIRED.** Empty-string defaults relocate the failure
   from loud-at-construction to confusing-at-first-use; declined. Test
   churn absorbed by one shared helper (conftest factory /
   TEST_OLLAMA_URL constant) — consolidating ~15 duplicate
   constructions is test hygiene, not cost.
3. **Debug flags: exempt via REGISTERED allowlist.** Acceptance-(a)
   amended: no UNREGISTERED os.getenv outside config.py; allowlist =
   {AGORA_STRUCTURE_STRICT, AGORA_SERIAL_TASKS, AGORA_DECISION_TIMEOUT,
   AGORA_RUN_OUTPUT_DIR}, each read site carrying a one-line exemption
   comment, plus a "Debug flags (env-only)" section in the config
   docs. AGORA_RUN_OUTPUT_DIR is additionally marked "candidate for
   promotion to Settings" in its comment.
4. **Split: three green commits as proposed** (2B-i harness env-free +
   builder + its scripts/tests; 2B-ii the 8 scripts + constructor
   defaults + test helper; 2B-iii precedence + conflict-warning +
   acceptance tests d/e).
