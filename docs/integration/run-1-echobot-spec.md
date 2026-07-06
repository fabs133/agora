# Integration run 1 — echobot (spec, human-authored planner artifact)

*Phase-1 artifact per docs/design/project-phases.md. Cast:
casts/p40-24gb.yaml (gemma implements; instruct verifies, non-blocking;
planner: human). Probe-shaped, headless, network-free.*

## Goal

A minimal Discord-style bot as a PURE command-router core with a thin
adapter. No real Discord API, no network, no token: the deliverable is
the core, its tests, and a CLI adapter proving end-to-end behavior.

## Functional spec

Package `echobot`. Core: `handle_message(text: str, rng: random.Random)
-> str | None` — pure function; returns None for non-command input.

Commands:
- `!ping` -> `pong`
- `!echo <text>` -> `<text>` verbatim (everything after the first space)
- `!roll NdM` (e.g. `!roll 2d6`) -> `rolled NdM: a+b+...=total`;
  rng injected (deterministic in tests); malformed spec -> usage message that MUST contain the substring "NdM" (acceptance predicate per F15; any phrasing otherwise)
- `!help` -> one line per command
- Unknown `!cmd` -> `unknown command: cmd (try !help)`

Adapter: `python -m echobot` reads lines from stdin, writes responses to
stdout, ignores None. No other IO.

## Required tests (named — decomposition postconditions check these exist)

test_ping, test_echo, test_echo_preserves_spacing, test_roll_deterministic
(seeded rng), test_roll_malformed, test_help_lists_all_commands,
test_unknown_command, test_non_command_returns_none.

## Phase plan (tasks execute under phase gates; pause at every boundary)

- **P3 scaffold** — T3.1: package skeleton (echobot/__init__.py,
  echobot/core.py stubs, echobot/__main__.py stub, tests/, requirements:
  pytest only). Gate: files exist; run_check `python -c "import echobot"`.
- **P4 implement core** — T4.1 router + ping/echo/help/unknown;
  T4.2 roll with injected rng. Gate: run_check `python -c "from
  echobot.core import handle_message"`; artifact checks per task.
  (Revision of P3 stubs: guard friction expected — watchlist item.)
- **P5 tests** — T5.1: write tests/test_core.py implementing exactly the
  named cases from this spec. Gate: run_check `pytest --collect-only -q`
  exit 0 AND all required test names present (contains-checks); then
  run_check `pytest -q` exit 0.
- **P6 integration** — T6.1: implement __main__ stdin/stdout adapter.
  Gate: run_check `pytest -q` still green.
- **P7 acceptance** — no new task; gate only:
  run_check `printf '!ping\n' | python -m echobot` stdout contains `pong`;
  same pattern for one !echo and one !roll (seed fixed via env or default).
- **P9 docs** — T9.1: README.md (what it is, how to run, how to test).
  Gate: contains-checks (run, test instructions present).
- **P8 repair** — on any red gate: ONE re-task from the repair template
  carrying the oracle output verbatim; re-run the gate; second red on the
  same gate = stop, record, chat-side decision. Repair budget: 1 per gate.

## Verifier protocol (recorded, non-blocking)

After each phase gate (green or red), one verifier task: instruct reads
the phase's artifacts + this spec's relevant section, emits
`{"phase": ..., "verdict": "pass|fail", "reasons": [...]}`.
Postcondition: parses as JSON with those keys. Verdicts are DATA for the
axis-2 casting decision; they gate nothing in run 1.

## What run 1 measures (pre-registered)

Per-phase gate outcomes; guard-friction handling (force/edit discovery);
cross-file naming drift at P5/P6; dependency behavior at P3; long-output
channel behavior on pytest failures; the oracle-fed repair cell (headline);
verifier verdict quality vs mechanical gates (agreement rate).
A clean one-shot run is welcome and not expected; every red gate that
produces a diagnosis is a success of the instrument.

---

## Amendment (2026-07-03): handoff for extensibility

**T9.2 (added to P9):** write PROJECT_STATE.md at the project root per
docs/integration/project-state-template.md, with this project's facts.
Gate: contains-checks for every mandatory template section header, plus
the recorded gate commands must be present verbatim (they are re-run in
any future phase 0). Verifier reviews T9.2 against the actual tree
(non-blocking, recorded). One-time human fact-check of the file map is
part of run-1 analysis — it measures whether the implementer can
describe its own project accurately.

**Run 2 (defined now, executed after run 1 analysis):** brownfield probe
on this project. Delta spec: (a) extend the command palette (e.g. !flip,
!choose a|b|c), (b) real Discord adapter as a NEW module against a fake
gateway with contract tests; core untouched. Phase 0 re-validation
opens the run; old gates = regression suite; live-server acceptance is
explicitly human (cannot be gate-checked headlessly and will not be
faked).
