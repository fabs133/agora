# Integration run 3 — echobot BROWNFIELD probe — session log

*Verbatim execution log. Binding spec/pre-registration:
docs/integration/run-3-brownfield-spec.md. Baseline: tag echobot-v1 (PROJECT_STATE.md
v1.1, fact-check PASS). Branch feat/integration-run-3 (cut off feat/integration-run-2).
Campaign: campaigns/integration-run-3.yaml. Flow:
flows/integration-run-3-brownfield.flow.yaml. Provenance (untracked):
runs_out/integration-run-3/.*

## Provenance note — starting state
Copied the echobot-v1 workspace tree (from tag echobot-v1 / the run-2 artifact at
runs_out/integration-run-2/echobot/echobot) to runs_out/integration-run-3/echobot/echobot
(echobot/ package, tests/, prose/, README.md, requirements.txt, PROJECT_STATE.md v1.1;
excluded .git/__pycache__/verdicts). git-init'd the workspace + committed the baseline
(151a901). Fresh ledger (runs_out/integration-run-3/, no run-2 bleed). Cast p40-24gb
unchanged; implementer allowlist stands (F13); tester owns tests/.

## Runner delta — --phase0 <artifact> (item 1)
run_phased.py gains `--phase0 <PROJECT_STATE.md>`: parse_verification_run_checks (F20
form) -> execute each run_check in the project dir -> write a P0 PhaseGateRecord
(mechanical-marked) -> print the standard gate report. Unit test: a fixture artifact
with one passing + one failing check -> the P0 record reflects BOTH (gate red, both
run_check records captured). --status surfaces the ledger-only P0 ahead of the task phases.
