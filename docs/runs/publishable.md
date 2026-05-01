# Publishable threads in the Agora archive

This document flags what in the archive is paper-shaped — i.e. could
support an external write-up — and what additional work each candidate
would need before it could ship. Lives next to
[findings.md](findings.md) so the archive doesn't read as purely
retrospective.

Each candidate gets:

- **Thesis** — the one-sentence claim.
- **Evidence already collected** — what the archive already supports.
- **Additional work needed** — what's not yet here.
- **Target venue / format** — where this could land, and what the
  reader would be.

---

## Candidate 1 — Cross-tier framework leverage on a fixed task

**Thesis**: For a fixed code-generation task (the URL shortener
6-deliverable brief), a deterministic-gate framework like Agora produces
a measurable progression in *what fails* across model tiers — from
capability-bound (qwen2.5:7b loops in retry for 62 minutes) to
plan-quality-bound (gpt-4o-mini ships structurally-defective code) to
model-judgment-bound at the value level (gpt-4o passes structural gates
but fails on test-vs-impl tuple-shape disagreement). The framework's
*value* is therefore tier-dependent: load-bearing scaffolding for weak
models, ergonomic correctness-substrate for medium-tier models,
diminishing-returns for high-tier interactive work.

**Evidence already collected**:
- Identical DAG executed on three tiers, captured in
  [findings.md §4.1](findings.md). Three runs cited:
  [url-shortener-mvp.run1-7b-broken.md](url-shortener-mvp.run1-7b-broken.md),
  [url-shortener-mvp.run7-4omini-typo](registry.yaml) (one of five 4o-mini runs),
  [url-shortener-mvp.live.md](url-shortener-mvp.live.md).
- Plan-builder cross-tier comparison ([findings.md §4.2](findings.md))
  with [plan-builder.run14-4omini-clean.md](plan-builder.run14-4omini-clean.md)
  as the 4o-mini reference and `plan-builder.live` as the 4o counterpart.
- Discord-bot single-tier framework progression
  ([findings.md §4.3](findings.md)) — same 7B model, evolving framework,
  Run 1 (6/11) → Run 13 (12/12 first-pass, 0 loopbacks) →
  Run 17 (23/23, multi-module).
- Failure taxonomy ([findings.md §1](findings.md)) categorising every
  observed failure mode across tiers; "what didn't" log
  ([findings.md §3](findings.md)) of approaches that turned out not to
  work.
- Decision log ([findings.md §7](findings.md)) of every hypothesis
  tested and kept/dropped with the run that drove the decision.

**Additional work needed**:
- **14B-class data point.** Per [findings.md §6.2](findings.md) — the
  middle of the range (qwen2.5:14b) is empirically untested. The
  cost-vs-capability story is incomplete without it. Pending hardware
  (~24 GB VRAM, May 2026 timeline per
  `project_paused_for_hardware.md`).
- **Tests-pass-against-planted-impl predicate** ([findings.md §6.1](findings.md))
  — would close the value-level gap that gpt-4o hit. Worth implementing
  as a paper artefact even if not strictly required for publication, to
  show the gap is closeable.
- **More than one brief.** The URL-shortener brief is the only one
  with cross-tier executor data. Replicating the comparison on
  Discord-bot or FastAPI-CRUD briefs would either confirm the pattern
  generalises or surface a new failure mode that the URL-shortener
  brief happens to avoid.

**Target venue / format**:
- **Workshop paper / pre-print** at a venue that cares about
  agentic-software-engineering empirics — *NeurIPS LLM Agents Workshop*,
  *ICLR Tiny Papers*, or similar. ~6–8 pages. The ablation tables and
  the failure taxonomy fit naturally; the framework code is open and
  reviewable.
- **Blog post / technical essay** for a more general audience. The
  one-graph version: "what failure mode you hit depends on your model
  tier, not just your prompt — here's a controlled study". Less rigour,
  more reach.
- **Hiring conversation artefact**. Pointing at this archive is
  evidence of the kind of empirical work the candidate (you) is capable
  of producing alone in a short window.

---

## Candidate 2 — Velocity of independent agentic-software development

**Thesis**: 48 archived runs across three test-bed projects, three model
tiers, two meta-flows (plan-builder, code-review), and four progressive
framework hardening rounds — produced in 11 days of independent
development by one developer with one capable-agent collaborator. The
velocity is part of the story. Specifically: deterministic gates +
structured archival + a capable interactive agent collaborator changes
what one person can produce in 11 days at a software-research level.

**Evidence already collected**:
- The archive itself: [registry.yaml](registry.yaml) with 46 dated run
  records, all timestamps cross-checked against workspace git history
  ([scripts/check_date_provenance.py](../../scripts/check_date_provenance.py)
  passes).
- Empirical span: workspace git active testing window 2026-04-16 →
  2026-04-22 (7 days); project lifetime 2026-04-15 → 2026-04-26 (11
  days inclusive).
- Test count drift: 501 → ~1090 across the post-2026-04-17 work (per
  [lessons-learned.md](../lessons-learned.md) "Post-2026-04-17 work").
- 18 framework rounds documented in lessons-learned.md's round-by-round
  evolution tables; each driven by a specific run that surfaced the
  bug.

**Additional work needed**:
- **A hours-of-attention estimate.** The 11-day calendar span isn't the
  same as 11 person-days of attention. A rough log of which days had
  active work would make this more credible. Currently buried in
  conversation history; would need extraction.
- **A dollar-figure baseline.** The framework's testing across three
  model tiers cost <$2 in API spend (per [findings.md §5.1](findings.md))
  — striking compared against, say, an academic GPU-cluster bill. Make
  this concrete.
- **A counter-factual.** The interesting framing isn't "11 days" alone;
  it's "11 days that produced what otherwise". Something like "without
  the agent collaborator and the deterministic-gate substrate, this
  archive would not exist; estimating how much longer it would take to
  reproduce manually" — open-ended, worth thinking about.

**Target venue / format**:
- **Methodology essay** — possibly the same outlet as Candidate 3
  (paired naturally; see below). Audience is people thinking about how
  AI tooling shifts the productivity ceiling for individual developers,
  particularly in research-software contexts.
- **Hiring conversation artefact** — concrete evidence of velocity at
  a quality bar that's auditable. Stronger than a portfolio screenshot
  because the archive supports drilling into any specific run.
- **Internal Anthropic / OpenAI / similar conversation** about where
  capable-agent collaboration changes what a single developer can build,
  with the archive as the corpus.

---

## Candidate 3 — Archival methodology for LLM-generated work products

**Thesis**: When an LLM generates a project archive (history doc,
findings doc, run register), the dangerous failure mode is
*confident-and-specific* assertions that propagate verbatim through
review. The defensive pattern is **provenance-tagged structure**:
every quantitative claim resolves to filesystem evidence
(`scripts/check_date_provenance.py`); every cost figure carries a
`source: recorded | estimated | unknown` tag (`registry.yaml` schema);
narrative claims cite verifiable run-ids
([findings.md citation-integrity verification](findings.md)). The
archive itself documents one instance of this pattern catching the
hallucination it was designed to catch.

**Evidence already collected**:
- [findings.md §8.1](findings.md) — the methodology note: a
  case study of the agent-drift incident on 2026-04-26 (claimed "ten
  weeks (2026-02 → 2026-04)" for an 11-day project), the user catching
  it, and the resulting defensive script that now ships with the
  archive.
- The registry's cost-provenance schema enforces `cost.source` per row.
  Inspectable in [registry.yaml](registry.yaml).
- [scripts/check_date_provenance.py](../../scripts/check_date_provenance.py)
  — small standalone verifier that cross-references date strings against
  workspace git ranges. Generalises to any project archive.
- [Memory note](../../../../.claude/projects/PROJECT/memory/feedback_verify_dates_and_counts.md)
  documenting the failure mode for future agent sessions.

**Additional work needed**:
- **Generalise the pattern.** The current archive's verifiers are
  Agora-specific (`workspace/.run*` dirs, registry YAML schema). The
  *pattern* (cost-provenance, date-provenance, citation-integrity,
  tone-match-against-original) is reusable — but no toolkit packages it.
  A small library or template repo "structured archives for
  LLM-generated work" would land the methodology in something
  installable.
- **More incidents to study.** One drift event is anecdote, not data.
  Either find more instances in this archive (worth grepping
  conversation history) or invite other practitioners to contribute
  their own cases.
- **Comparison with non-LLM archives.** The methodology pattern isn't
  novel in spirit — historians, journalists, and auditors do this. The
  novel claim is that LLM-generated archives need the pattern
  *especially* because the agents are confident-and-specific by default.
  Worth backing the comparison up explicitly.

**Target venue / format**:
- **Pairs naturally with Candidate 2.** Both are methodology
  observations from the same archival project; could land as a single
  longer essay with two threads. "What 11 days of capable-agent
  collaboration looks like — and what discipline it takes to keep the
  archive honest."
- **Anthropic / agent-tooling community conversation.** The archival
  pattern feeds directly into questions about what infrastructure
  capable-agent collaboration needs as it scales beyond single sessions
  — relevant to Claude Code, Agent SDK, and similar product surfaces.
- **Open-source toolkit.** Strip the Agora-specific bits, ship as a
  small Python package with date/cost/citation verifiers + a registry
  schema template. Low-effort, plausibly useful.

---

## Cross-cutting recommendation

The three candidates are not independent — Candidate 1 (cross-tier
framework) is the technical results, Candidate 2 (velocity) is the
context, Candidate 3 (methodology) is the meta-discipline that kept
the archive honest. They land most naturally as a *layered* write-up:
technical paper for the empirical results (Candidate 1), pointing at
the archive as primary evidence; methodology essay (Candidate 2 + 3)
for the broader audience, citing the technical paper as the worked
example.

If only one thread ships, **Candidate 1** is the obvious choice — it has
the most evidence already collected, the most conventional venue, and
the clearest "what would I add" backlog. Candidates 2 and 3 are
methodology observations that strengthen the technical paper if
included; they're also worth their own treatment but aren't the lead.

---

*This document is forward-looking — none of these threads has been
worked up beyond the archive itself. Adding a candidate or revising
this list is the kind of low-friction edit that benefits from being
in a markdown file rather than locked behind a heavier deliverable.*
