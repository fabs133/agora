# Agora — Run history archive

Structured record of the 46 runs catalogued from
[workspace/](../../workspace/) plus the cross-cutting analysis. Built
on 2026-04-26 as the project's archival snapshot.

> If you're reading this for the first time, start with
> [findings.md](findings.md). It synthesises everything; the registry
> and narratives are evidence for the claims it makes.

## Layout

```
docs/runs/
├── README.md                         ← you are here
├── registry.yaml                     ← 46 runs, mechanically extracted
├── registry_notes.yaml               ← hand-filled per-run notes (merged into registry on regeneration)
├── findings.md                       ← cross-cutting analysis (8 sections)
├── publishable.md                    ← what's paper-shaped in the archive (Candidates 1–3)
├── _inventory.csv                    ← working file used during build (safe to delete)
├── url-shortener-mvp.run1-7b-broken.md   ← per-run narrative ×5
├── discord-bot.run3.md
├── discord-bot.run13.md
├── url-shortener-mvp.live.md
└── plan-builder.run14-4omini-clean.md
```

## Reading order by use case

### "I need to understand what Agora does and what worked"

1. [lessons-learned.md](../lessons-learned.md) — the original 2026-04-17
   snapshot + appended Post-2026-04-17 work. Architecture, load-bearing
   ideas, file map.
2. [findings.md §2](findings.md) — what worked, ranked by evidence.
3. [discord-bot.run13.md](discord-bot.run13.md) — what working looks like
   on a weak local model.

### "I'm picking the project back up after time away"

1. lessons-learned.md "Closing note for future-me" + the new
   "Post-2026-04-17 work" section.
2. [findings.md §6](findings.md) — open framework gaps with the run
   that surfaced each.
3. [findings.md §7.3](findings.md) — open hypotheses, what's awaiting
   what.
4. The five narratives, in this order:
   [run1-7b-broken](url-shortener-mvp.run1-7b-broken.md) →
   [run3](discord-bot.run3.md) →
   [run13](discord-bot.run13.md) →
   [run14-4omini-clean](plan-builder.run14-4omini-clean.md) →
   [live (gpt-4o)](url-shortener-mvp.live.md). They span from
   capability-ceiling to framework-asymptote in 11 days of project
   evolution.

### "I'm curious about a specific run"

1. Look up the `run_id` in [registry.yaml](registry.yaml).
2. If it has a `notes:` field, that's the narrative summary.
3. If a `<run_id>.md` file exists, read that for the full story.
4. The `dir_path` and `log_paths` fields point at the raw artefacts.

### "I want to write this up externally"

[publishable.md](publishable.md) lists three candidate threads with
thesis, evidence, additional work needed, and target venue. Cross-tier
empirical comparison (Candidate 1) is the most paper-shaped.

## Regenerating the archive

```bash
.venv/Scripts/python.exe -X utf8 scripts/extract_run_metadata.py
```

That one command writes both `_inventory.csv` and `registry.yaml` from
the workspace artefacts plus the hand-filled notes in
`registry_notes.yaml`. Idempotent — same inputs produce identical
outputs.

For provenance verification:

```bash
.venv/Scripts/python.exe -X utf8 scripts/check_date_provenance.py
```

Flags any ISO date string in the archive that doesn't resolve to either
a workspace git commit, the repo creation date, or today.

## Verification status (2026-04-26)

| Check | Status |
|---|---|
| Extractor idempotent (same inputs → identical YAML) | ✓ |
| Citation integrity (every `run_id` in findings.md / narratives resolves in registry.yaml) | ✓ |
| Date-provenance (every date resolves to filesystem evidence) | ✓ |
| Cost-provenance schema enforced (`source: recorded \| estimated \| unknown` on every cost) | ✓ |
| Tone match (lessons-learned.md added section reads continuous with the original) | ✓ |
| Test suite untouched (1090+ tests still passing; archive only adds `scripts/` and `docs/`) | ✓ (no framework code modified) |

## Provenance disclaimer

Every cost figure in this archive is **estimated** unless explicitly
tagged otherwise. Source: tier-level estimates from session memory
($0.025/run for gpt-4o-mini, $0.40/run for gpt-4o). The registry's
`cost.source` field marks each value individually. To upgrade to
recorded values, the LiteLLM cost-tracker output needs to land in
log lines — see [findings.md §6.4](findings.md).

Every date claim in this archive resolves against workspace git
history per [scripts/check_date_provenance.py](../../scripts/check_date_provenance.py).
The methodology note at [findings.md §8.1](findings.md) documents the
one date hallucination caught during the archival session itself.
