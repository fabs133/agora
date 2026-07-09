# Capability program — reconciled build plan (bench pipeline → exchange)

*Drafted 2026-07-09. Reconciles [roles-and-casting.md](roles-and-casting.md)
"Stage 3: benchmark pipeline" (Layer 1) with
[capability-exchange.md](capability-exchange.md) (Layer 2) into ONE ordered,
dependency-correct plan. Planning artifact — no code yet. Each stage lists its
dependency, the concrete work, and a binding acceptance test. Owner decisions
are collected in §5; none of them block writing this plan, but several block
the stages that reference them.*

---

## 1. The one-sentence shape

**Layer 1 turns `pull model → one command → a keyed, re-derivable capability
matrix`; Layer 2 is the community distribution layer for that matrix.** They are
not two systems — Layer 2 re-runs Layer 1's *same validator* over contributed
records. The single most important structural rule in both docs: **the bench
validator, the exchange CI check, and the `agora contribute` packager are ONE
implementation in the agora package** (the F1/F2 anti-drift lesson). Every stage
below is arranged so that seam is never duplicated.

## 2. Current state — what exists vs what's missing

Layer 1's prerequisites are *further along than the "not built" label suggests* —
the derivation math is done; the keying, storage, command, and battery are not.

**Exists (load-bearing, reused):**
- `agora.observe.layer2.capability_vectors()` — a *pure, locked-schema*
  capability-vector derivation from `tasks_df`/`runs_df`
  (`campaign, model, strategy, axis, sub_target, raw_value, normalized_score,
  repeats, excluded_repeats, ci_low, ci_high`). Plus `reproducibility_by_cell`,
  `classify` (the 4 behavioural classes), `model_metrics`. This IS the vector engine.
- The campaign/probe harness: `run_campaign` / `expand_campaign` / `run_phased`,
  the tool-call-fidelity probe, staged execution → `run.jsonl` / `tasks.jsonl`.
- `RunRecord` already carries `ollama_version` (daemon), `git_commit`, `harness`
  (dict), `probe_version`, `strategy`.
- `/api/show` plumbing (`vram.py`) — the model **digest** is one field away.
- `agora.fleet.cast`: `validate_cast` (rules 1–4) + `resolve_cast`; the
  `casts/p40-24gb.yaml` envelope with free-text `evidence: {campaign, gate}`.

**Missing (the actual work):**
1. **The re-derivable key.** Vectors today are keyed `campaign+model+strategy`.
   The matrix key both docs mandate is
   `(model_digest, battery_version, probe_version, harness_hash, daemon_version)`
   + hardware/date metadata. Gap: `model_digest` (not captured — only the tag),
   `battery_version` (no battery concept), `harness_hash` (harness is an
   un-hashed dict). `probe_version` ✓, `daemon_version` ✓ (`ollama_version`).
2. **Battery format** (`benchmarks/standard-v1.yaml`) — a named, versioned
   probe bundle.
3. **`agora bench <model-tag>`** — digest extraction → campaign-from-battery →
   run → layer2 → append keyed row.
4. **The matrix store** — a DERIVED, rebuildable index (§5 decision: format).
5. **`agora cast eligible <role>`** + upgrading `validate_cast` rule 3 from
   free-text evidence to *matrix-row* citations at a compatible key.
6. **All of Layer 2** — schema, packager/validator/sanitizer, exchange repo + CI,
   read path, seed/gate.

Note: roles-and-casting **Stages 0–2 are already done** (cast validate/load
exists; integration run 1 shipped). Layer 1 here == roles-and-casting **Stage 3**.

## 3. Dependency graph

```
        layer2.capability_vectors (EXISTS)   /api/show (EXISTS)   campaign harness (EXISTS)
                        \                         |                   /
                         \                        |                  /
   L1-A  Re-derivable key + matrix store  <-------+-----------------
                         |
   L1-B  Battery format + `agora bench`   (needs L1-A key)
                         |
   L1-C  `cast eligible` + rule-3 upgrade (needs L1-A, L1-B)
                         |
   =============  LAYER-1 COMPLETE: the bench pipeline  =============
                         |
   L2-0  Exchange schema + repo skeleton + license  (needs key+vector shape frozen by L1-A/B)
                         |
   L2-1  `agora contribute`: packager + SHARED validator + sanitizer  (needs L1-B output)
                         |
   L2-2  Exchange CI: validate + re-derive + index + conflicts        (needs L2-1 validator)
                         |
   L2-3  Read path: `agora exchange sync` + cache + pinned-ref casts   (needs L1-C, L2-2)
                         |
   L2-4  Seed + release gate: dogfood P40 rows; one outsider lands a green PR
                         |
   L2-5  (optional) static index page; HF mirror
```

The re-derivable **key (L1-A)** is the true foundation: it is what makes the
vector *comparable*, what the matrix is indexed on, what the exchange re-derives
against, and what a cast pins. Build it first and precisely; everything keys off it.

## 4. Stages (ordered, with acceptance)

### Layer 1 — the bench pipeline (roles-and-casting Stage 3)

**L1-A — Re-derivable key + matrix store.**
Define and document the canonical key
`(model_digest, battery_version, probe_version, harness_hash, daemon_version)`
plus metadata columns (hardware, quantization, date, git_commit). Capture what's
missing: `model_digest` from `/api/show`; `harness_hash` = a documented canonical
hash of the effective harness config (the fields that change behaviour:
tool_errors, nudge/review/salvage budgets, routed_retry/max_task_retries).
Extend the vector step so a matrix row carries the FULL key (either enrich
`capability_vectors` or add a keyed wrapper that joins run-level identity onto
each vector). Stand up the matrix store as a DERIVED, rebuildable index over the
`runs_out` JSONL (source of truth), with comparability enforced at query time
(cross-key pooling requires an explicit flag, never silent).
*Acceptance:* given a campaign's JSONL, derive matrix rows carrying the complete
key; delete + rebuild is byte-identical (idempotent); a row missing any key
field is rejected, not defaulted; a cross-key query without the opt-in flag errors.

**L1-B — Battery format + `agora bench`.**
`benchmarks/standard-v1.yaml`: the tool-call-fidelity probe at its current
`probe_version` under two arms (production: corrective + review_budget 1; raw
control), 3 repeats each; `battery_version` is part of every row's key.
`agora bench <model-tag>`: auto-roster (manifest/template/**digest** extraction,
scripting the Phase-1 procedure) → generate a campaign from the battery → run
staged → layer2 → append keyed rows to the matrix.
*Acceptance:* a never-seen local model goes `pull → agora bench → matrix rows`
with zero manual steps besides review; re-running the same model+battery is a
no-op or an explicit new dated row (decision §5.4), never a silent duplicate key.

**L1-C — Eligibility + cast rule-3 upgrade.**
`agora cast eligible <role>`: query the matrix against the role's requirements
(roles.yaml requirements expressed against matrix columns), output candidate
profiles with their evidence rows. Upgrade `validate_cast` rule 3: a binding
cites *matrix rows* at a compatible `(probe_version, harness_hash)` satisfying
the role's requirement, OR a waiver — replacing the free-text `{campaign, gate}`.
Migrate `casts/p40-24gb.yaml` (dual-accept during transition, then require rows).
*Acceptance:* `cast eligible implementer` lists candidates+rows from the matrix;
`cast validate` passes on the migrated p40 cast citing real rows and fails loud
when a cited row is absent or key-incompatible; the existing 1454-test suite
stays green (cast tests updated, not weakened).

### Layer 2 — the exchange (distribution)

**L2-0 — Schema + repo skeleton + license.**
`agora-capability-exchange` repo: versioned `schema/`, the `contributions/…` and
`index/` layout, `CONTRIBUTING.md`, license (§5.5). The submission schema mirrors
the L1-A key + L1-B vector shape (now frozen).
*Acceptance:* a hand-built example submission validates against the schema.

**L2-1 — `agora contribute`: packager + shared validator + sanitizer.**
Package a bench output dir into a submission (manifest + gzipped records +
attestation, auto-gathered from provenance) using the SAME validator L2-2 runs.
Sanitize machine-private strings (usernames, hostnames, absolute paths — the
registry.yaml lesson) with a printed scrub report. `--dry-run` first; open a PR
via `gh` if present, else emit a ready-to-push branch + instructions.
*Acceptance:* packaging a real P40 bench output produces a submission that
validates AND contains zero machine-private strings (grep-audited), dry-run only.

**L2-2 — Exchange CI: validate + re-derive + index + conflicts.**
GitHub Actions installs agora at a PINNED RELEASE TAG and runs the shared
validator: schema-valid, JSONL internally consistent, **vector re-derived from
the raw records and required to match the manifest**, plausibility lints. Build
`index/matrix.csv` (derived, never hand-edited); detect conflicts →
`index/conflicts.md`; count reproductions at identical keys.
*Acceptance:* a submission with a tampered vector is REJECTED by re-derivation;
two agreeing submissions at one key increment the reproduction count; two
disagreeing ones land in conflicts.md (never silently averaged).

**L2-3 — Read path: sync + cache + pinned-ref casts.**
`agora exchange sync` (shallow clone/pull → local cache); `cast eligible` and
`validate_cast` query local matrix UNION exchange matrix, each row carrying
provenance (local vs community, reproduction count, exchange ref). Casts declare
an evidence policy (`{allow_community, min_reproductions}`) and pin the exchange
COMMIT SHA. Offline-first: cache serves when the network doesn't.
*Acceptance:* a cast citing `exchange@<sha>` validates offline from cache and
fails loudly on a missing/again-unresolvable ref; the exchange is never a hard
runtime dependency.

**L2-4 — Seed + release gate.**
Dogfood: the P40's own battery rows become the first contributions. Gate (mirrors
SETUP.md's outsider gate): ONE OUTSIDER, following the exchange docs alone,
reproduces a battery on their hardware and lands a green PR.
*Acceptance:* the outsider PR merges on green CI with a re-derived row from a GPU
the maintainer does not own — the marketplace's phase-0.

**L2-5 — (optional, later).** Static GitHub-Pages render of `index/`; HF dataset
mirror. Machine-readable index is canonical; these are cosmetic/reach.

## 5. Owner decisions (surface now; each gates its stage)

1. **Matrix store format — CONFLICT in the two docs.** roles-and-casting says
   `capability-matrix.sqlite`; capability-exchange's index is `index/matrix.csv`.
   **DECIDED (owner, 2026-07-09): CSV is canonical** — git-diffable, exactly what
   the exchange distributes, re-derivable. An optional local SQLite built FROM the
   CSV is allowed later for fast `cast eligible` queries, but the CSV is the one
   format that crosses the Layer-1/Layer-2 boundary and is the source index. (Gates L1-A — RESOLVED.)
2. **`harness_hash` definition.** **DECIDED (owner, 2026-07-09):** the hash is
   over the behaviour-affecting subset of the effective harness config ONLY —
   `tool_errors`, `nudge_budget`, `review_budget`, `salvage_budget`,
   `routed_retry_budget`, `max_task_retries` — canonicalized as sorted-key JSON
   and SHA-256'd (a short prefix for the key). Endpoints/paths/credentials are
   NEVER in the hash. The exact field set is pinned in `harness_hash()` and
   documented alongside it. (Gates L1-A — RESOLVED.)
3. **Battery v1 contents.** Confirm v1 = tool-call-fidelity probe only, 2 arms ×
   3 repeats; and the policy for adding probes later (new battery_version, never
   mutate v1). (Gates L1-B.)
4. **Re-bench collision policy.** Same (model_digest, battery, harness, daemon)
   re-run: no-op, replace, or append a dated reproduction row? *Recommendation:*
   append dated rows and let reproduction-count logic (L2-2) treat local repeats
   the same as community ones. (Gates L1-B.)
5. **Exchange data license.** **DECIDED (owner, 2026-07-09): MIT** — for both the
   framework and the data/exchange repo. (NOTE: the framework's current LICENSE is
   Apache-2.0; switching it to MIT is a deliberate relicense — flagged, not yet
   applied.) (Gates L2-0 — RESOLVED for the exchange repo.)
6. **Repo/CI ownership + release cadence.** The exchange CI pins an agora RELEASE
   TAG; that means Layer 2 needs a tagged agora release (ties into the
   `v0.1.0` tag already flagged in the onboarding audit). Confirm the exchange is
   a *separate* public repo the maintainer owns. (Gates L2-2.)
7. **Sequencing.** Recommend building **L1-A → L1-B** (freezing key + vector +
   battery) BEFORE opening L2-0, so the submission schema is grounded and cannot
   churn. L1-C can proceed in parallel with L2-0/L2-1. (Sequencing call.)

## 6. Scope fences (from capability-exchange, carried here)

No leaderboard website (static index render is optional L2-5). No accounts
(GitHub identity). No raw integration-run archives — bench batteries only, 5 MB
compressed per-submission cap. Perfect fraud-proofing is NOT claimed (attestation
is testimony; CI re-derivation makes fabrication expensive, reproduction counts
make disagreement visible).

## 7. Out of scope (recorded, not forgotten)

- `registry.yaml` path relativization (already its own queued chore; the
  sanitizer in L2-1 shares its lesson but not its code).
- Proxy/corporate-network model-pull failures — doc note at most.
- macOS/ARM Conduit/daemon image verification beyond "multi-arch" —
  accept-and-monitor; first issue report decides.

---

### Recommended first move

If you approve this sequencing: start at **L1-A** (the re-derivable key + matrix
store), because it is the foundation every other stage is indexed on and it is
small and self-contained — it mostly *keys and stores* an output (`layer2`
vectors) that already exists. Decisions §5.1 and §5.2 gate it and are the two I'd
want settled before writing that code.
