# `discord-bot.run13`

**Reference green run.** "What working looks like" on a weak local
model. 12 tasks, zero loopbacks, ~5 minutes wall-clock. lessons-learned.md
calls this its reference for the Discord-bot test bed.

| Field | Value |
|---|---|
| Model | qwen2.5:7b (Q4_K_M, local Ollama) |
| Project | discord-bot |
| Date | 2026-04-17 18:06 → 18:10 (per git timestamps) |
| First-pass green | **12/12** |
| Outcome | DONE first-pass |
| Loopbacks | **0** |
| Duration (lessons-learned.md) | 316s, 47k / 2.4k tokens |
| Cost | $0 (local) |
| Log | **not preserved** in `workspace/.logs/`; narrative draws on lessons-learned.md Run 13 row + the produced artefacts in the run dir |
| Run dir | [workspace/discord-bot.run13](../../workspace/discord-bot.run13/) |

## Setup

Same Discord-bot test bed as
[discord-bot.run3](discord-bot.run3.md): 12-task DAG, 7 staged
tasks, three slash commands (`help_lookup`, `roll_command`,
`register_command`) on a discord.py bot. The brief lives in
[scripts/run_discord_bot_test.py](../../scripts/run_discord_bot_test.py).

The contrast with `run3` is the *framework* state, not the model. Run 3
was on Round-1 framework (postconditions only). Run 13 was on a Round-11
framework: `save_as`-atomic fetch (Round 5), `output_path` banner
(Round 6), distiller for large reads (Round 7), three edit primitives
(Round 8), artifact snapshot + write-event cards (Round 9), reactions
parser + reply parser (Round 10), `_find_code_after_main_block` AST
detector (Round 11).

Same model, eleven rounds of framework hardening between.

## What happened

The 12 tasks completed first-pass. lessons-learned.md's run table reports
zero loopbacks. The git history of the run dir shows:

```
d15cb88 auto: wrote bot.py
10cc5c2 auto: wrote bot.py
18a65d1 auto: wrote test_bot.py
9482eb1 auto: wrote bot.py
bc29b9f auto: wrote README.md
508db06 auto: wrote requirements.txt
c61cfd8 auto: wrote bot.py
56e8b7d auto: wrote requirements.txt
7fe44c9 auto: wrote bot.py
ff31b7d auto: wrote design/modules.md
61994c6 auto: wrote design/commands.md
6291a8e auto: wrote kb/commands.md
73d7836 auto: wrote kb/intro.md
9af2266 chore: initialize discord-bot
```

14 commits, all `auto:` prefix from the auto-hook. `bot.py` was written
or edited five times across the run — each one a separate task that
added a slash command, with the edit primitives (`replace`,
`insert_before`, `append`) keeping every revision diff-clean.
Compare against `run11`, where re-emitting bot.py via `write_file`
produced the `discord.InterACTION` typo that drove Round 8's edit
primitives.

The produced bot.py is 28 lines, the produced test_bot.py is 14 lines —
small, focused, no scope creep. The kb/ files (commands.md, intro.md)
are committed cleanly with no truncation, validating that the post-Round-5
`save_as` path keeps a 67 KB doc atomic on the storage boundary.

## What worked

- **Zero loopbacks.** The framework is built around assuming retries
  are normal and absorbing them. A run with zero loopbacks is the
  asymptote — what happens when every gate is correct, every edit is
  unique-anchored, every fetch is atomic, and the model behaves.
- **Edit primitives held.** Five revisions of bot.py across the run, no
  transcription bugs. Run 11 had `discord.InterACTION` after re-emitting
  25 lines via `write_file`. Run 13 used `edit_file_replace` /
  `edit_file_insert_before` / `edit_file_append` and stayed clean.
- **Auto-hooks fired silently 14 times** — every write triggered
  `check_python` → `git_commit` automatically. The model didn't have to
  remember; the framework took care of it. The git log is the audit
  trail.
- **Read-heavy tasks via the distiller.** The `kb/commands.md` file is
  67 KB; the `read_file` of it would overflow num_ctx. Round 7's
  hierarchical map-reduce distiller summarises on read, so downstream
  design tasks consumed a digest, not the raw file.

## What broke

Nothing. That's the point of a reference run — it shows the mechanism
working without anything failing. lessons-learned.md Run 13 row:
`12/12 · 0 loopbacks · 316s · 47k/2.4k`.

## What changed in the framework as a result

This run did not drive a framework change. It is the *evidence* that the
framework works. Specifically:

- It is the canonical "what working looks like" in [findings.md §2](findings.md)
  — referenced in the "Postconditions are the ground truth" entry as the
  case where every gate passed first-pass without intervention.
- It is the reference green run for the Discord-bot domain in the
  cross-tier table at [findings.md §4.3](findings.md). Same model,
  evolving framework, scaling tasks — that's the project's main
  argument, and Run 13 is the inflection where it landed.
- It seeded the scale-up to Run 17 (`discord-bot-full.live`) — the
  multi-module 23-task variant on the same hardware. Run 13 going green
  on a 7B was the empirical evidence that motivated trying the bigger
  DAG.

## See also

- [findings.md §2](findings.md) — what worked, ranked by evidence; this
  run cited as the postconditions reference.
- [findings.md §4.3](findings.md) — Discord-bot test bed framework
  progression.
- [lessons-learned.md](../lessons-learned.md) Run 13 row + the closing-note
  framing "the framework works. The framework generalizes. Build on it."
- [discord-bot.run3.md](discord-bot.run3.md) — same project, pre-Round-5,
  the cascade.
- [url-shortener-mvp.run1-7b-broken.md](url-shortener-mvp.run1-7b-broken.md) —
  same model, *without* staging, the capability-ceiling failure mode.
