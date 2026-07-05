# Integration run 2.0 — echobot clean greenfield — session log

*Verbatim execution log. Pre-registration:
docs/runs/integration-run-2/pre-registration.md (binding, F1-F14 accumulated
fixes). Run 1.x is CLOSED. Branch feat/integration-run-2 (cut off
feat/integration-run-1 after run 1.5). Campaign: campaigns/integration-run-2.yaml.
Provenance (untracked): runs_out/integration-run-2/.*

## Pre-flight (2026-07-05) @ run-2 prep committed (suite green 1449, ruff clean)
```
ollama /api/version: {"version":"0.31.1"}; gemma4:e4b + qwen2.5:7b-instruct resident
campaign: integration-run-2.yaml — same flow (amended through 1.5), fresh output_dir
  runs_out/integration-run-2/ (fresh workspace at .../echobot), harness {corrective,
  nudge 1, review 0}, cast p40-24gb, params identical to run 1 (temp 0, seed 42, ctx 8192).
ledger (--status on the fresh dir): P3..P9 all PENDING, next=run P3 — no run-1 bleed.
```
Execution: full P3 -> P9 via `--next` per phase; standard protocol (one repair per
gate, second red on the same gate stops, waivers forbidden). Gate reports below.
