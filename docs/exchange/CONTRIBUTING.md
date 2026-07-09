# Contributing to the capability exchange

*In-repo draft (capability-program L2-0). This moves to the root of the
`agora-capability-exchange` repo when it is created; it lives here now so the
schema and the validator that enforces it evolve together in one place.*

The exchange is a community record of model **evidence**. You run the standard
bench battery on your hardware and contribute the raw records; CI re-derives the
capability vector from them. **A contribution is never a claimed score** — it is
evidence, and a row that cannot be re-derived is rejected mechanically.

## How to contribute (three commands)

```bash
# 1. Benchmark a model you have pulled locally.
agora bench <profile>            # runs the battery, writes runs_out/bench/<battery>-<profile>/

# 2. Package it into a submission (dry-run first — prints the scrub report + validates locally).
agora contribute runs_out/bench/<battery>-<profile> \
    --digest sha256:<from `ollama show`> --battery standard-v1 \
    --contributor <your-gh-user> --gpu "<your GPU>" --os "<your OS>"

# 3. Re-run with --write, then open a PR adding the printed submission directory.
agora contribute ... --write
```

`agora contribute` runs the **same validator the CI runs** and **sanitizes**
machine-private strings (username, hostname, home path) before anything leaves
your machine — read the printed scrub report.

## What a submission is

```
contributions/<model_digest12>/<battery>@p<probe>/<contributor>-<date>-<id>/
    manifest.yaml       the claim: the key + the derived vector rows
    runs.jsonl.gz       the raw run records (sanitized)
    tasks.jsonl.gz      the raw task records (sanitized)
    attestation.yaml    environment testimony (GPU, driver, daemon, digest, OS)
```

**Key.** Every vector row is keyed by
`(model_digest, battery_version, probe_version, harness_hash, daemon_version)`.
The **model digest** (from `ollama show`), never the tag — the same tag can be
re-pushed; the digest is the identity. Cross-key pooling is impossible: each row
carries its full key.

## The trust model (layered)

1. **Mechanical (CI, blocking).** Schema-valid; the vector is **re-derived from
   the raw records and must match the manifest**; plausibility lints. Green CI =
   mergeable. Fabricating a number means fabricating coherent raw records, not a
   value.
2. **Attestation, not proof.** The environment block is your testimony, labeled
   as such. The exchange does not verify hardware it cannot touch.
3. **Reproduction as currency.** Independent submissions at the same key that
   agree raise a row's reproduction count; disagreement is surfaced (hardware /
   FP nondeterminism at community scale is *data*), never silently averaged.
4. **Trust decided at consumption.** A cast declares an evidence policy
   (`{allow_community, min_reproductions}`) and pins the exchange commit SHA, so
   a cast citing community evidence stays reproducible.

## Rules

- Bench batteries only — no raw integration-run archives. Per-submission cap:
  5 MB compressed.
- Do not hand-edit `manifest.yaml` or `index/`; the manifest is derived and the
  index is CI-built.
- Data license: **Apache-2.0** (owner decision) — the exchange repo ships an
  Apache-2.0 `LICENSE`, matching the framework.
