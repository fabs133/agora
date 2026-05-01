# `discord-bot.run3`

**Fetch cascade — drove the most consequential framework change.** This
run is the origin of the `save_as` primitive (Round 5).

| Field | Value |
|---|---|
| Model | qwen2.5:7b (Q4_K_M, local Ollama) |
| Project | discord-bot |
| Date | 2026-04-16 18:09 → 18:17 (~8 minutes wall-clock per git timestamps) |
| First-pass green | 9/12 |
| Outcome | FAILED (cascade) |
| Loopbacks | 1 |
| Cost | $0 (local) |
| Log | **not preserved** in `workspace/.logs/`; narrative reconstructed from lessons-learned.md Round 5 entry and the run's git history |
| Run dir | [workspace/discord-bot.run3](../../workspace/discord-bot.run3/) |

## Setup

The Discord bot test bed (12-task DAG, 7 staged) — three slash commands
on a discord.py bot, with a knowledge base fetched from the discord.py
docs at runtime. Brief lives in
[scripts/run_discord_bot_test.py](../../scripts/run_discord_bot_test.py).

The fetch step at this point in the project's history was: model calls
`fetch_url(url)`, the framework returns the page text as a tool result,
the model is then expected to call `write_file('kb/<x>.md', content=...)`
to persist it. Two-step round-trip through the model's tool-call surface.

## What happened

The 67 KB `kb/commands.md` file (the discord.py commands documentation)
came back from `fetch_url` cleanly, but on the *write_file* round-trip the
model truncated it. The `write_file` succeeded, but with a truncated
payload. Subsequent tasks that read `kb/commands.md` got a partial
reference document and proceeded to make up missing API names.

A single LLM flake on `fetch_commands` cascaded through the whole DAG
because every downstream "design X" and "implement X" task depended on
the truncated KB file. The framework's postconditions were structural —
file_exists, file_contains for sentinels — and the truncated file passed
both. The code that emerged compiled and imported but referenced
methods that didn't exist on `discord.commands`.

Loopback retried `fetch_commands` once. Same flake. Cascade reproduced.

## What worked

- **The cascade was visible in the artefacts.** Even though the framework
  shipped broken code, the failure surfaces concretely: the truncated
  `kb/commands.md` is on disk in the run dir, and the downstream
  hallucinations (made-up method names) are in the produced bot.py.
  Diagnosing the cause from the artefacts alone is straightforward.
- **One run was enough to demonstrate the pattern.** The fix didn't
  require a survey or a property test; it was visible in a single
  failure mode.

## What broke

- **Round-trip atomicity.** Anything that flows through the LLM's
  working memory as a tool result has a non-zero truncation/edit
  probability. For 67 KB documents on a 7B with limited num_ctx,
  non-zero is too high.
- **Retries-inside-the-model don't help.** Loopback re-runs the task
  from scratch but the model's failure mode is correlated across runs:
  it truncates 67 KB documents reliably, just at slightly different
  positions.

## What changed in the framework as a result

**Round 5** of the Round-by-round table in
[lessons-learned.md](../lessons-learned.md): the `save_as=...` parameter on
`fetch_url`. With `save_as`, the fetch step writes the response body
*directly* to the storage path; the LLM never sees the content as a
tool argument. Atomicity at the storage boundary, not retry semantics
inside the working-memory loop.

Bounded HTTP retries on transient errors landed in the same change for
genuinely network-flake cases —
[fleet/web_fetch.py](../../src/agora/fleet/web_fetch.py) `fetch_url`.
Re-running discord-bot Run 9 after the change cleared the cascade and
the run hit 10/12 user-approved.

The general principle that came out of this run is summarised in
[findings.md §3 "What didn't"](findings.md):

> Fetch retries before save_as: rounds 3–4 tried to add HTTP retries
> inside the fetch tool. Couldn't fully solve the cascade because the
> fetched content still had to round-trip through the LLM as a tool
> argument and the LLM truncated it. Round 5's `fetch_url(save_as=...)`
> made the fetch atomic and the cascade stopped.

> **Verdict**: retry alone isn't enough when the value flows through the
> model's working memory; atomicity at the storage boundary is.

## See also

- [findings.md §3](findings.md) — "What didn't" entry on fetch retries.
- [findings.md §7.1](findings.md) — Decision log entry for `save_as`.
- [lessons-learned.md](../lessons-learned.md) Round 5 + the load-bearing-idea
  framing of why atomicity at the storage boundary matters.
- [discord-bot.run13.md](discord-bot.run13.md) — same project, same
  model, post-Round-5 — 12/12 first-pass.
