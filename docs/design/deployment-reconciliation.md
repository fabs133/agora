# Deployment reconciliation — baseline, corrections, execution harness

*2026-07-15. Trigger: first clean end-to-end run (P3→P9, 0 repairs,
~32 min, Python 3.14.3, max_tokens 4096, salvage 1) + the divergence
collection (A/B/C/D tables). Order is binding: the baseline is
recorded before anything else changes.*

## Phase 0 — record the baseline (FIRST, nothing else before it)

Copy the scratchpad clone's session artifacts into the repo:
docs/runs/lifecycle-baseline/session-log.md (config block, per-phase
table, wall-clock, the live bot transcript) + findings Part 17 in the
integration findings file: first clean run, fix-stack-as-a-stack,
verifier series update (V3.1 valid first-ever; V5/V6 still malformed
at 4096 — envelope does NOT explain the verifier gap), the 2
reasoning-only turns at P9 with S7 never needed. Tag the capturing
commit `lifecycle-baseline-1`. Provenance rule applied to itself: the
findings entry cites the EFFECTIVE-params log, not the campaign file.

## Phase 1 — corrections sweep (living docs + memory)

1. **GPU claim**: grep-and-fix every "3060 Ti" reference (README,
   SETUP, anywhere) → Tesla P40. Only the P40 ever ran end-to-end.
2. **Hash remediation** (filter-repo rewrote all hashes; tags survive:
   echobot-v1→957be3f4, echobot-v2→15edd7c9):
   - Check .git/filter-repo/commit-map; if present, commit it as
     docs/history/commit-map.txt and mechanically remap hashes in
     LIVING docs (README, SETUP, arc.md, design docs).
   - Historical session logs / findings parts are NOT rewritten: one
     banner at the top of the findings file + a README history note
     ("hashes cited in historical documents predate the 2026-07
     history rewrite; tags survive; map at ...").
   - Claude Code memory: all commit anchors → tags + new hashes.
3. **OLLAMA.md**: mark device-specific header ("author's reference box
   — not general guidance"), fix B2 (MAX_LOADED_MODELS=1 wrong for the
   validated cast: 14.6 GB co-residency fits 24 GB; =1 causes
   evict/reload thrash on every verifier task) and fold in D1 (WinNAT
   reserved-range port collision + AGORA_OLLAMA_BASE_URL workaround —
   which validated the single-source config design live).
4. **review-timeout landmine**: config.py default 86400 → sane default
   (300 s); kill the three-way inconsistency (config 86400 / harness
   dead-300 / docstring false-300); fix the demo docstring's
   auto-approve claim (actual: task-aware _auto_fallback).
5. **F26 (new): config files are moving targets.** Doctrine: findings
   and reports cite effective-params provenance, never current file
   state (B9: run 2.0 executed 2048 while the file now says 4096).
6. **Pre-register the F14-at-P4 envelope A/B** (2048 vs 4096, T4.2
   only, n=3 each) — F14's P4 clause is NOT rewritten until this runs.

## Phase 2 — execution harness (deployment mode; design, minimal)

One code path: extend run_phased with `--auto` (advance-while-green
loop around the existing single-phase execution). NO new runner.
- Loop: while frontier has phases → execute next; GREEN → continue;
  RED → STOP with the full gate report (repairs remain operator
  actions; a gate that stops the run is the feature, not a limitation).
- Preflight: programmatic doctor call at start (Ollama + Conduit +
  cast models + VRAM) — fixes the C-finding that the runner's
  preflight never checked Conduit.
- Fix C1/C2: build_matrix_client wrapped in asyncio.wait_for(8 s) AND
  gated on the observer flag (currently unconditional — a hard Matrix
  dependency even with the observer off). Disambiguate in code
  comments: JSONL/run.log provenance is UNCONDITIONAL (F3); the Matrix
  surface is the optional human live-view.
- Effective-config + per-phase gate reports appended to the session
  log automatically (the baseline run's report shape, mechanized).
- Acceptance: (i) one command from a clean workspace reproduces the
  baseline (P3→P9 green, no operator action); (ii) kill-Conduit test:
  fails fast with a named red line, never hangs (the 3-minute silent
  hang is the regression case); (iii) --next per-phase mode unchanged
  (testing workflow preserved).

## Phase 3 — SETUP.md rewrite around the truth

The documented demo becomes the VALIDATED path: the echobot lifecycle
via `run_phased --auto` on the validated cast — the exact run the
baseline just proved (32 min, DONE = all gates green, live bot
transcript as the success snippet). Hardware stated honestly:
validated on a 24 GB P40; co-residency needs ~15 GB + ctx headroom;
smaller-hardware cast = open item, not a promise. The discord demo:
demoted to "alternate quick demo" ONLY after its fixes land (stdout
re-wrapping removed so output streams; timeout default sane) — or cut
from SETUP entirely if the fixes don't make it crisp; owner's call at
review. Fix A1 (digest via /api/tags, not ollama show), A2 (interpreter
selection guidance + tested-on note: 3.12 floor, 3.14 validated), A7
(OLLAMA.md pointer reframed), A8 (compose version key), A9 (port-
collision troubleshooting entry incl. WinNAT reserved ranges).

## Phase 4 — resume the release pass (R1–R5 as briefed)

Unchanged, now on corrected ground: merge, v0.1.0 at the merge commit,
public-main front-door smoke (against the REWRITTEN SETUP), scrub
verification (post-filter-repo history is new — rerun the history
scan), CI matrix, exchange checks, housekeeping. The uncommitted demo
review-timeout fix from the earlier pass lands in Phase 1 item 4.

## Backlog (recorded, not now)

mark_complete re-emission loop (turns 9–20 identical) → joins the
rejection-stall detector item; `mkdir_directory` unknown-tool emission
→ roster quirk note; smaller-hardware demo cast; F18''' caveat datum
(a concrete code fix hit a reasoning-only turn with salvage DISABLED —
re-examine only with salvage armed, config-correct).

---

## C2 ruling (owner, 2026-07-15): Matrix becomes optional — option (a)

run_phased's observer-off runs were building clients and creating rooms
nobody consumed (orchestrator:1093 skips only dispatcher/renderer/
review-coordinator). Decision: NullMatrixClient over the client
protocol; client AND Conduit preflight both gated on enable_observer.
Core documented path becomes Python + Ollama only; Conduit moves to an
optional "live observation view" section in SETUP (the interactive
demo still requires it when exercised).

Guardrails (binding):
1. Per-tool null semantics, no default void: post_note -> run.log
   entry + visible "recorded to log" result (F3; the verifier fidelity
   series flows through it); request_review -> LOUD unavailable error,
   never silent; view-only methods may no-op. Enumerate every
   Matrix-touching inner tool and define each.
2. Visibility: startup line announcing observer-off + provenance
   unaffected; doctor reports Conduit "skipped (observer off)" — a
   distinct state from green.
3. Acceptance: (i) full lifecycle green with Docker entirely DOWN
   (current outage = the fixture, verify now); (ii) observer-ON run
   behaviorally unchanged (verify when stack returns); (iii) test:
   no real client construction anywhere when the flag is off
   (orchestrator:1093 is the named regression).

---

## C2 acceptance-(i) reframed + crash protocol (owner, 2026-07-15)

**(i) as originally written is retracted as doctrinally defective:** it
bet a harness acceptance on a model property measured to be
non-deterministic across sessions (identical seed drew differently
than the baseline — consistent with the near-tie/state-dependence
findings; no new number). Reframed and MET: "one command drives the
lifecycle without Conduit, reports skip-vs-green honestly, and stops
only on genuine gate reds." Demonstrated: Docker dead, [SKIP] conduit,
P3/P4 green on Ollama alone, null semantics live (post_note->log,
request_review->loud refusal, gate passed), --auto stop on genuine
red. The stopped draw additionally reproduced F14's whole-file-rewrite
signature in the wild (repair fixed the named defect, dropped !roll)
— recorded as corroborating data, not harness failure. Re-rolling for
a green was declined by the executor and that refusal is ratified.

**Phase 3 addition:** SETUP troubleshooting entry distinguishing
"install broken" from "gate stopped a defective draw" — the latter is
designed behavior with a gate report; the doc must teach this or every
stopped run becomes a support issue.

**Host-crash protocol (0xC0000409 x2, Claude Code process, not this
repo):** timeboxed evidence capture (crash-dialog logs + Windows Event
Viewer fail-fast entry naming the faulting module) -> upstream bug
report -> update the host if newer. Standing ops rule, codified in
OLLAMA.md + memory: long-lived services start DETACHED (Start-Process),
never from tool subprocesses — the only variable between the crash
that took the stack down and the one that didn't. No project time on
debugging the host binary beyond the timebox.

---

## R1 authorization + v0.1.0 scope ruling (owner, 2026-07-15)

**v0.1.0 = the validated surface only.** Merge chore/integration-
hardening -> main (--no-ff); tag v0.1.0 at the merge commit; flip the
repo PUBLIC; then the front-door smoke (fresh clone of public main,
SETUP verbatim through doctor). The bench branch is NOT merged for the
release: it forked pre-hardening, is unreviewed, and would regress the
Stage-2 invariants on contact. It ships as v0.2.0 after rebase onto
new main + passing the hardening acceptance greps, which are hereby
MERGE GATES for every future branch (and go into CI as a lint job:
os.getenv-outside-config, localhost-literals, Settings-import
allowlist).

**Exchange re-point (design-consistent):** CI splits into (1) schema
gate — pure jsonschema, zero agora dependency, active now against the
seeded example; (2) re-derivation gate — pinned agora tag, ACTIVATES
AT v0.2.0. Exchange README states contributions open with v0.2.0 when
the re-derivation trust mechanism exists. The marketplace does not
open before its trust gate does.

**Demo demotion ratified** (fix-gates met, labelled NOT VERIFIED, no
unearned figures); backlog: verify e2e someday, then it may earn a
runtime figure.

---

## v0.1.0 tag ruling + immutability rule (owner, 2026-07-15)

**Ruling: option (a), executed once.** Sequence: (1) fix the two
surviving README 3060 references (status + Known Limitations) and
re-run the Phase-1 sweep with the BROAD pattern (grep -ri "3060"
repo-wide) — variant phrasing is how two instances survived a narrow
grep; acceptance greps are executed literally and broadly. (2) Move
v0.1.0 to that commit as an ANNOTATED tag whose message states it was
re-pointed pre-announcement (fix for the doctor Conduit-red blocker +
doc corrections); force-push the tag; recreate any GitHub Release
object off the new ref.

**Standing rule, effective immediately after this move:** tags are
immutable once announced or plausibly consumed. The pre-announcement
window is minutes, not days; outside it, fixes are new versions,
always. This precedent expires the moment it is used.

Ratified from the smoke: the SKIP fix + deliberate inversion of the
mandatory-Matrix test (the old test encoded the contract this release
overturns); the ASCII-not-cp1252 output test (cp1252 would have passed
the very character that broke); the interpreter-selection note's
immediate payoff (3.11.9 caught at step 1 instead of a bare pip
failure at step 3).
