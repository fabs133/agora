# Run 3 — brownfield probe: pre-registration and delta spec

*Registered 2026-07-05, before any prep or execution. Baseline: tag
`echobot-v1` (PROJECT_STATE.md v1.1, fact-check PASS). Naming: runs
1.x-2.x were the greenfield program; run 3 is the brownfield probe
designed in docs/design/project-phases.md (Phase 0 + deepened Phase 9)
and docs/integration/run-1-echobot-spec.md (extension amendment).*

## Purpose

The question the casting/handoff architecture exists to answer: can a
small-context implementer RETURN to a completed project — re-establish
truth mechanically, navigate via the brief instead of full-project
context, extend without breaking, and hand off again. Headline
measurement: brief-as-index navigation (does the model read
PROJECT_STATE.md / the file map before editing — observable in run.log).

## Starting state

Copy the echobot-v1 workspace tree to runs_out/integration-run-3/echobot
(provenance note in the session log: copied from the tagged baseline).
Fresh ledger. Cast p40-24gb unchanged; implementer allowlist stands
(revisions go through write_file force under the F13 invariant); tester
owns tests/.

## Delta spec (extension only; core contract remains frozen)

New commands, both rng-injected and deterministic under seed, each with
its F15 acceptance predicate:
- `!flip` -> exactly "heads" or "tails" (rng.choice); seeded-deterministic.
- `!choose a b c` -> exactly one of the space-separated arguments
  (rng.choice); no arguments -> usage message that MUST contain "choose".
- `!help` MUST list both new commands (existing predicate style).

Discord adapter, transport-injected (no network, no discord.py, no token):
- `echobot/discord_adapter.py`: a minimal Gateway protocol
  (receive() -> message events with .content; send(channel, text)) and
  `run_adapter(gateway, rng)` mapping events through handle_message —
  None responses are not sent. Core is NOT modified by this module.
- Contract tests against a FakeGateway (scripted events, captured
  sends) — deterministic, headless.
- Live-Discord wiring is explicitly HUMAN, post-run, non-gated.

## Phase plan

- **P0 — re-validation (gate-only).** Runner executes the run_checks
  parsed from PROJECT_STATE.md (F20 form). RED-TEAM FIRST, before the
  real run: stale one source byte (modify a usage string in core.py) ->
  P0 MUST red -> restore -> P0 green. Both recorded in the session log.
  A protective gate is only trusted after it has been seen failing.
- **P4' — extend core.** One task, delta contract inline (the two
  commands + predicates above), pointer to the map; NOT the old spec.
  Gates: existing smoke gates still green + new smokes
  (`handle_message('!flip', Random(0))` in {heads,tails};
  `'!choose a b c'` result in {a,b,c}).
- **P5' — extend tests.** Tester adds named tests (test_flip_deterministic,
  test_choose, test_choose_no_args, test_help_lists_new_commands);
  gate: names present + full suite green — the OLD 8 tests are the
  regression suite, free of charge.
- **P6' — adapter.** New module per the delta spec; gate: contract
  tests green + full suite green + `import echobot.discord_adapter`.
- **P7' — acceptance.** CLI unchanged (!ping still pong — stdin checks)
  + one FakeGateway round-trip run_check.
- **P9' — handoff v2.** Re-extract FACT (file map + capability
  inventory changed); prose micro-asks ONLY for sections whose content
  changed (extension_points, conventions if touched), anchored per F21;
  assemble PROJECT_STATE.md v2; fact-check follows chat-side.

## Pre-registered worlds and watchlist

Worlds: (a) clean or one-repair-per-gate completion -> PROJECT_STATE v2
-> fact-check -> program milestone (arc doc + push). (b) any gate
double-reds -> stop, standard findings. (c) P0 red-team fails to red ->
STOP EVERYTHING — the handoff's protective claim is false; that finding
outranks the run.
Watchlist (from project-phases.md, now live): stale-brief trust,
navigation failure (edits the wrong file despite the map), regression
breakage (old tests red), convention drift (sentence-case strings, test
naming — now honestly recorded in the brief). Protocol: one repair per
gate, second red stops, waivers forbidden, conditions-defect
re-establishment rule stands.

## Explicitly out of scope

Live Discord connection; new roles; unmeasured tools; core signature
changes (frozen per the brief the model itself wrote).
