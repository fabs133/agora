# Onboarding pre-mortem — audit brief (pre-push release criterion #2)

*2026-07-09. Companion to integration-hardening Stage 6: the walkthrough
validated the procedure on the dev box; this audits the environment
axes a stranger diverges on (OS, console, Python, GPU vendor, git
config, network topology). Every item is a mechanical check or a
bounded fix. Execute as one pass; report per item: OK / FIXED(commit) /
FLAGGED(owner decision).*

## A — blockers (fix before push)

A1 LICENSE file exists at repo root. If absent: STOP and flag — owner
   must choose (this is a legal gate, not a technical one).
A2 Python floor: read requires-python; verify install + suite on the
   floor version (not just 3.14). If 3.14-only-isms exist, either fix
   or raise the floor DELIBERATELY and state it in README+SETUP.
A3 Bare "python" argv anywhere run_check/flows/gates execute the
   interpreter -> resolve sys.executable at the run_check boundary.
   Grep: '"python"' in flows/, src/agora/plan/, campaign templates.
A4 ASCII-safe console output for agora doctor + the demo script's
   prints (the run_phased ascii-safety precedent + test). Grep doctor/
   demo for non-ASCII glyphs.
A5 Workspace git identity: repo_manager sets local user.name/email
   (agora@local) on workspace init, so auto-hook commits never depend
   on the user's global git config. Test: init in an env with
   GIT_CONFIG_GLOBAL pointed at an empty file; write+commit succeeds.
A6 .gitattributes: pin eol for byte-exact fixture paths (and sensible
   repo-wide defaults) so autocrlf variants cannot alter fixtures at
   checkout. Verify: fresh clone with core.autocrlf=true on Windows
   AND =input on Linux -> byte-equality tests still green (at minimum:
   reason through and pin; full matrix if cheap).
A7 Non-NVIDIA path: doctor's VRAM check degrades to a WARNING with a
   CPU-mode note when nvidia-smi is absent; never a blocking red.
A8 matrix-nio dependency is plain (no [e2e]/libolm). Verify pyproject.

## B — documentation lines (cheap, high leverage)

B1 SETUP.md top block: hardware minimum (VRAM for the demo model),
   download size, expected demo runtime on stated reference hardware,
   and a "what success looks like" sample output snippet.
B2 Bold line: the demo needs NO Discord account and touches no network
   beyond localhost (the script name invites the question).
B3 Pin the demo model tag exactly; record the VALIDATED digest
   (ollama show) in SETUP; state expected result (DONE 12/12) and that
   a different digest may score differently -> troubleshooting note.
   (Same-tag-not-same-weights, aimed at strangers.)
B4 WSL note: Ollama on Windows host + code in WSL ->
   AGORA_OLLAMA_BASE_URL=http://<host>:11434.
B5 docker compose (v2) syntax everywhere; port-6167 collision hint in
   doctor's Conduit red line if not already present.
B6 Verify .env.example registration token matches conduit.example.toml
   (two files, one handshake) — and add a comment in each pointing at
   the other.

## C — release mechanics

C1 Tag v0.1.0 at the push commit; README states where to report issues.
C2 README top: one-paragraph what-this-is + hardware line + SETUP link
   (a stranger decides in 30 seconds whether this repo is for them).

## Out of scope (recorded, not forgotten)

- registry.yaml path relativization (already queued as its own chore;
  not on the user path).
- Proxy/corporate-network model-pull failures (doc note at most).
- macOS/ARM Conduit image verification beyond "image is multi-arch"
  (accept-and-monitor; first issue report decides).
