# Capability exchange — architecture sketch (design only)

*Drafted 2026-07-09. Goal: a community marketplace of model evidence —
users run the bench battery on their hardware, contribute results,
everyone's casting decisions get cheaper. Prerequisite: the Stage-3
benchmark pipeline (`agora bench`, battery files, matrix derivation) —
the exchange is its distribution layer, not a separate system.*

## Core principle

**Evidence in, scores derived.** A contribution is never a claimed
score; it is the raw run records plus attestation, and the exchange's
CI re-derives the capability vector from the submitted JSONL. A row
that cannot be re-derived is rejected mechanically. This is the
program's provenance doctrine at community scale — and it is the
entire trust model's foundation.

## Storage: a public GitHub repo (separate from the framework)

`agora-capability-exchange` — contributions arrive as pull requests.

Why git/GitHub over the alternatives:
- vs a hosted service/API: no server, no accounts, no cost, and the
  maintainer is one person. Review gate = PR; provenance = commit
  history; immutability = SHAs. Boring wins.
- vs a Hugging Face dataset repo: viable alternative (LFS, dataset
  viewer, community reach) and a cheap LATER mirror; GitHub is
  canonical because the CI (validate + re-derive + index) is the
  load-bearing piece and Actions is where it lives.

Layout:

    schema/                     submission schema, versioned
    contributions/<model_digest12>/<battery>@<probe>/<gh-user>-<date>-<id>/
        manifest.yaml           the claim: full evidence key + vector
        runs.jsonl.gz           the raw records (bounded, see caps)
        attestation.yaml        environment: GPU, driver, daemon
                                version, model DIGEST from /api/show,
                                quantization, OS
    index/matrix.csv            DERIVED by CI — never hand-edited
    index/conflicts.md          DERIVED — disagreeing reproductions

**Keys.** A row is keyed by (model_digest, battery_version,
probe_version, harness_hash, daemon_version); hardware is metadata.
Model DIGEST, never tag — same tag is not same weights (the
daemon-drift lesson, community scale). Cross-key pooling is impossible
by construction: the index carries the full key on every row.

## Trust model (layered, consumer-decided)

1. **Mechanical (CI, blocking):** schema-valid; JSONL internally
   consistent; vector RE-DERIVED from the raw records and required to
   match the manifest; plausibility lints (durations, token counts,
   digest formats). Green CI = mergeable; maintainer merge is a
   rubber stamp, not a review burden.
2. **Attestation, not proof:** the environment block is contributor
   testimony, labeled as such. The exchange does not pretend to verify
   hardware it cannot touch.
3. **Reproduction as currency:** independent submissions at the same
   key that agree increase a row's reproduction count; disagreement is
   surfaced in conflicts.md — a conflict is DATA (hardware/FP
   nondeterminism at community scale; the near-tie doctrine predicts
   some), never silently averaged.
4. **Trust decided at consumption, not centrally:** casts declare an
   evidence policy — e.g. `{allow_community: true, min_reproductions:
   1}` — and a cast citing community evidence pins the exchange
   COMMIT SHA in the citation, making the evidence reference immutable
   and the cast reproducible.

## Framework integration

**Read path — `agora exchange sync`:** shallow-clone/pull into a local
cache; `cast eligible` and `validate_cast` query local matrix UNION
exchange matrix, every row carrying its provenance (local vs
community, reproduction count, exchange ref). Offline-first: the cache
serves when the network doesn't; the exchange is never a hard runtime
dependency.

**Write path — `agora contribute <bench-output-dir>`:** packages a
submission (manifest + gzipped records + attestation, all
auto-gathered from provenance — everything needed is already recorded
by doctrine); runs the SAME validator CI runs (fail early, locally);
**sanitizes** machine-private strings (usernames, hostnames, absolute
paths — the registry.yaml lesson) and prints a scrub report; then
opens a PR via `gh` if present, else emits a ready-to-push branch plus
printed instructions. gh is a convenience, never a requirement.

**Validator placement:** in the agora package, one implementation; the
exchange CI installs agora at a PINNED RELEASE TAG. Shared code so the
gate and the packager cannot drift (the F1/F2 lesson).

## Scope fences (deliberately not building)

No leaderboard website (a static GitHub Pages render of index/ is a
cheap optional stage, machine-readable index comes first). No accounts
(GitHub identity suffices). No raw integration-run archives — bench
batteries only, with a per-submission size cap (5 MB compressed) so
the repo stays clonable for years. License decision required at repo
creation (recommend CC0 for data; flag for owner).

## Stages

- **S0** — schema + submission format finalized; exchange repo
  skeleton; license + CONTRIBUTING.md. Acceptance: a hand-built
  example submission validates.
- **S1** — packager + local validator + sanitizer in agora
  (`agora contribute`, dry-run mode). Acceptance: packaging a real
  P40 bench output produces a submission that validates and contains
  zero machine-private strings (grep-audited).
- **S2** — exchange CI: validate, re-derive, index build, conflict
  detection. Acceptance: a submission with a tampered vector is
  REJECTED by re-derivation; two agreeing submissions increment the
  reproduction count; two disagreeing ones land in conflicts.md.
- **S3** — read path: sync, cache, cast integration with pinned refs
  + evidence policy. Acceptance: a cast citing exchange@sha validates
  offline from cache and fails loudly on a missing ref.
- **S4** — seed + gate: the P40's own battery rows become the first
  contributions (dogfood), and the release gate mirrors the setup
  doc's: ONE OUTSIDER, following the docs alone, reproduces a battery
  on their hardware and lands a green PR. That PR is the exchange's
  phase-0 — the marketplace exists when the first row arrives from a
  GPU you don't own.
- **S5 (optional, later)** — static index page; HF dataset mirror.

## Risks, stated

- Nobody contributes: the exchange still pays for itself as YOUR
  matrix's public, immutable home — casts citing exchange@sha work at
  n=1 contributor.
- Junk contributions: CI re-derivation makes fabrication expensive
  (you must fabricate coherent raw records, not a number) and
  reproduction counts make it visible; perfect fraud-proofing is
  explicitly not claimed (attestation is testimony).
- Repo growth: bounded by caps and bench-only scope; archive strategy
  is a problem worth having and deferred until it exists.
