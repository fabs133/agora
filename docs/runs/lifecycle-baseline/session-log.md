# Lifecycle baseline — first clean end-to-end run (P3→P9) — session log

*Verbatim execution record, 2026-07-15. The first single-session,
zero-repair traversal of the full echobot lifecycle in this project's
history. Every prior "complete" traversal was a multi-run program: run 2
reached `next: done` only at run 2.5, after six executions across two days
with code and flow fixes committed between each (see
`docs/runs/integration-run-2/session-log.md`). This run executed the
accumulated fix stack **as a stack**, on a fresh ledger, in one session.*

> **Provenance rule, applied to this document.** Configuration below is
> quoted from the runner's **effective-params log**, not from the campaign
> file. Campaign YAML is a moving target: `campaigns/integration-run-2.yaml`
> today reads `max_tokens: 4096`, but run 2.0 *executed* at 2048 — the 4096
> was written back after the run-2.3 envelope experiment. The effective log
> is the only citable record of what actually reached the model.

## Conditions (quoted from the run's own output)

```
[*] effective endpoints: ollama=http://localhost:11700  matrix=http://localhost:6167  profiles=profiles.yaml (cwd)
== agora doctor ==
[ OK ] ollama: reachable at http://localhost:11700 (version 0.31.1)
[ OK ] ollama-models: all 4 cast model(s) present
all checks passed.
[*] effective params [ollama/gemma4:e4b]:          num_ctx=8192* max_tokens=4096* temperature=0.0* seed=42*  (*=campaign override over profile)
[*] effective params [ollama/qwen2.5:7b-instruct]: num_ctx=8192* max_tokens=4096* temperature=0.0* seed=42*  (*=campaign override over profile)
[*] Logging into Conduit as @agora:agora.local
```

| | |
|---|---|
| Commit | `5ab8950` (`chore/integration-hardening`, `echobot-v2-24-g5ab8950`) — `echobot-v2` is an ancestor, so the full run-2.5 + run-3 fix stack is present |
| Campaign | `campaigns/integration-run-2.yaml` (fresh `output_dir`, ledger P3..P9 all pending — no bleed) |
| Flow | `flows/integration-run-1-echobot.flow.yaml` (amended through 2.5: inline contracts, F10 smoke gates, F15 `NdM` predicate, T9.2a–d micro-asks) |
| Cast | `casts/p40-24gb.yaml` — implementer + tester `gemma-e4b`, verifier `instruct`, planner human |
| Harness | `{tool_errors: corrective, nudge_budget: 1, review_budget: 0, salvage_budget: 1}` |
| Models | `gemma4:e4b` `c6eb396dbd5992bb` 9.6 GB Q4_K_M · `qwen2.5:7b-instruct` `845dbda0ea48ed74` 4.7 GB Q4_K_M |
| Daemon | Ollama **0.31.1**, `OLLAMA_MAX_LOADED_MODELS=2` (both cast models co-resident, 14.6 GB / 24 GB) |
| Hardware | Tesla P40 24 GB (GPU 0) |
| Interpreter | **Python 3.14.3** — above the `>=3.12` floor; not previously exercised by this program |
| Protocol | `--next` per phase; one repair per red gate; second red on a gate stops; waivers forbidden |

> **Endpoint note.** Ollama ran on `:11700`, not the default `:11434`, because
> port 11434 falls inside a Windows WinNAT reserved range (11420–11519) on this
> box and cannot be bound. Relocation was a one-line `.env` change
> (`AGORA_OLLAMA_BASE_URL`) — an unplanned live exercise of the single-source
> config design, which absorbed it with no code change.

## Result — all six gates green, zero repairs

| Phase | Gate | Blocking tasks | Verifier | Evidence |
|---|---|---|---|---|
| P3 | **GREEN** | T3.1 | V3.1 **pass** | artifacts + `python -c import echobot` → exit 0 |
| P4 | **GREEN** | T4.1, T4.2 | V4.1 **pass** | `assert handle_message('!ping', Random(0)) == 'pong'` → exit 0; `assert 'rolled 2d6:' in handle_message('!roll 2d6', Random(0))` → exit 0 |
| P5 | **GREEN** | T5.1 | V5.1 *fail* | `pytest --collect-only -q` → 8 collected; `pytest -q` → **8 passed** |
| P6 | **GREEN** | T6.1 | V6.1 *fail* | `pytest -q` → 8 passed; `python -m echobot` → `pong` |
| P7 | **GREEN** | T7.1 | V7.1 **pass** | 3/3 CLI acceptance (`pong`, `hello world`, `rolled 2d6: 6+4=10`) |
| P9 | **GREEN** (mechanical re-eval) | T9.1, T9.2a–d | V9.1 **pass** | 8 assembled headers + `pytest -q` + `python -m echobot` |

```
=== integration-run-2 — phase status ===
  P3 green | P4 green | P5 green | P6 green | P7 green | P9 green  (mechanical re-eval)
next: done (all phases green or waived)
```

Verifier failures at P5/P6 are **non-blocking by design** (verifier tasks never
gate); they are recorded, not waived. No waiver was issued anywhere in this run.

## Wall-clock

| Phase | Start | Elapsed |
|---|---|---|
| P3 | 12:43:20 | 4m 49s |
| P4 | 12:48:09 | 6m 31s |
| P5 | 12:54:40 | 6m 58s |
| P6 | 13:01:38 | 5m 53s |
| P7 | 13:07:31 | 3m 32s |
| P9 | 13:11:03 | 4m 42s |
| **Total** | **12:43:20 → 13:15:45** | **32m 25s** |

## Per-task provenance (`tasks.jsonl`, executed records)

```
task   phase role         blk status  first_pass iters struct malformed unknown_name
T3.1   P3    implementer  T   passed  T          20    20     0         0
V3.1   P3    reviewer     F   passed  T          5     4      0         0
T4.1   P4    implementer  T   passed  T          5     4      0         0
T4.2   P4    implementer  T   passed  T          6     5      0         0
V4.1   P4    reviewer     F   passed  T          3     2      0         0
T5.1   P5    tester       T   passed  T          3     2      0         0
V5.1   P5    reviewer     F   failed  F          5     5      0         0
T6.1   P6    implementer  T   passed  T          4     3      0         0
V6.1   P6    reviewer     F   failed  F          3     2      0         0
T7.1   P7    implementer  T   passed  T          7     6      0         4
V7.1   P7    reviewer     F   passed  T          4     3      0         0
T9.1   P9    implementer  T   passed  T          5     4      0         0
T9.2a  P9    implementer  T   passed  T          4     3      0         0
T9.2b  P9    implementer  T   passed  T          3     2      0         0
T9.2c  P9    implementer  T   passed  T          3     2      0         0
T9.2d  P9    implementer  T   failed  F          3     2      0         0
V9.1   P9    reviewer     F   passed  T          3     2      0         0

23 records = 17 executed + 6 mechanical (T9.1, T9.2a-d, V9.1)   <- F17b: mechanical re-eval records persist
statuses (executed): 14 passed, 3 failed (V5.1, V6.1 nonblocking; T9.2d passes on mechanical re-eval)
salvages_used = 0 | turns_reasoning_only = 0 | nudges_used = 0 | tool_calls_malformed = 0   (ALL 23 records)
```

`T9.2d`'s executed record reads `failed` because its postconditions include the
assembled `PROJECT_STATE.md` headers, which are evaluated **before** the runner
assembles `PROJECT_STATE.md = mechanical FACT + the four prose files`. The
post-assembly mechanical re-eval passes — the `(mechanical re-eval)` tag on the
P9 gate. This is the designed assembly order, not a masked failure.

## Fix-stack evidence (each fix verified live, together, in one run)

- **F13** (allowlist × overwrite-guard invariant): the allowlist filtered 13
  tools on all **10 implementer tasks** (T3.1, T4.1, T4.2, T6.1, T7.1, T9.1,
  T9.2a–d) and **not one** logged `hid write_file`. The hide fired only on
  unrestricted seats — the **tester** (T5.1) and every verifier (V3.1, V4.1,
  V5.1, V6.1, V7.1, V9.1). Both behaviours correct, exactly as run 2.0 recorded.
- **F6 / spec-channel** (tester writes real tests, not self-mocks):
  `tests/test_core.py` opens `from echobot.core import handle_message`, contains
  **zero** mock/monkeypatch/stub references, and calls the real function under a
  seeded `random.Random`. World (a).
- **F15** (`NdM` acceptance predicate): the implementation's malformed-roll reply
  is `Usage: !roll NdM (e.g., !roll 1d6). Malformed specification.` — carries the
  literal `NdM` substring. `test_roll_malformed` asserts it. Green first pass; no
  spec amendment needed this run.
- **F16 / F17** (adapter import contract + no-swallow): `echobot/__main__.py`
  line 3 is `from echobot.core import handle_message`; there is **no bare
  `except`** anywhere in the file. Both defects that stopped runs 2.1→2.2 are
  simply absent on the first pass.
- **F18''' / task design** (concrete micro-asks): T9.2a–d each passed first try,
  `turns_reasoning_only=0`, `salvages_used=0`. **S7 was armed
  (`salvage_budget: 1`) and never needed.**
- **F19** (param provenance): the effective-params log shows `max_tokens=4096*`
  reaching **both** models (`*` = campaign override over profile).
- **F17b** (mechanical re-eval records persist): 6 mechanical records written.
- **F10** (behavioural smoke gates): the `!ping` and `!roll 2d6` asserts are what
  actually proved P4, not a file-contains check.

## The artifact — live behaviour

The bot the framework built, executed directly:

```
$ printf '!ping\n!roll 2d6\n!help\n' | python -m echobot
pong
rolled 2d6: 5+5=10
Available commands:
  !ping - Returns 'pong'.
  !echo <text> - Repeats the given text verbatim.
  !roll NdM - Rolls N dice with M sides (e.g., !roll 20d6).
  !help - Shows this help message.
```

Full command surface, verified against the real module:

```
'!ping'              -> 'pong'
'!echo hello  world' -> 'hello  world'          (interior spacing preserved)
'!roll 2d6'          -> 'rolled 2d6: 4+4=8'     (regex r"(\d+)d(\d+)", rng injected)
'!roll xyz'          -> 'Usage: !roll NdM (e.g., !roll 1d6). Malformed specification.'
'!bogus'             -> 'unknown command: bogus (try !help)'
'plain text'         -> None
```

## `PROJECT_STATE.md` — fully model-authored

Zero `(human)` placeholders. Prose files: `architecture.md` (533 B),
`conventions.md` (268 B), `extension_points.md` (371 B), `how_to_run.md` (175 B).

Model-authored architecture prose (T9.2a), reproducing the spec invariants
independently:

> The core logic function, handle_message, MUST remain a pure function,
> accepting only inputs and returning output without performing any I/O
> operations. All input/output handling (e.g., reading from stdin, writing to
> stdout) MUST be confined exclusively to the main execution adapter
> (`__main__`). The random number generator (rng) MUST be injected as an
> argument to ensure deterministic behavior when seeded with random.Random.
> The core function signature MUST remain fixed:
> `handle_message(text: str, rng: random.Random) -> str | None`.

Mechanical FACT (extractor, correct-by-construction from the AST):

```
## Capability inventory

`echobot/core.py`:
- `def handle_message(text: str, rng: random.Random) -> str | None`
```

## Comparison to the recorded history

| Gate | Prior record | This run |
|---|---|---|
| P3 | green, 1 nudge; **V3.1 never once passed** | green, 0 nudges, **V3.1 valid — first ever** |
| P4 | run 2.0 **red** (T4.2 dropped `!roll` entirely) → 1 repair → green | **green, first pass** |
| P5 | run 2.0 **red** (2 failed / 6 passed) → repair → red → **STOP**; cleared only via F15 amendment + re-establishment across 2.0→2.1 | **green, first pass, 8/8** |
| P6 | green only after 2.0→2.1 prep | **green, first pass** |
| P7 | run 2.1 **red** (F16/F17) → repair → red → **STOP**; green first at 2.2 | **green, first pass, 3/3** |
| P9 | F18 family; blocked 2.2→2.4; green only at 2.5 after the micro-ask split | **green, all four first try** |
| **Program cost** | **6 executions, 2 days, ~5 repairs, spec amendments, code fixes between runs** | **1 session, 32 min, 0 repairs, 0 amendments** |

## Deviations and caveats (recorded, not resolved here)

1. **Python 3.14.3, not 3.12.** No 3.12 exists on this box. The floor is
   `>=3.12`; 3.14 is *above* it but was never previously exercised. The full
   lifecycle passed on it.
2. **Prewarm ignores the pinned `num_ctx`.** `ollama ps` showed both models
   loaded by the prewarm at **32768**; the first real task call reloaded gemma at
   the pinned **8192**. Generations ran at 8192 — the cost is a redundant 9.6 GB
   load in wall-clock, not fidelity, on this path. (Known: prewarm-ignores-num-ctx.)
3. **`OLLAMA_MAX_LOADED_MODELS=2`, not the `=1` in `OLLAMA.md`.** The p40-24gb
   cast needs 9.6 + 5 = 14.6 GB co-resident, which fits 24 GB. `=1` forces an
   evict/reload of gemma on every verifier task. `OLLAMA.md`'s `=1` rationale
   ("24 GB can't hold two of the *larger* models") does not apply to this cast.
4. **T7.1 logged 4 unknown-tool-name events** (`tool_call_unknown_name`) yet
   passed — roster quirk, backlog.
5. **`duration_s` is 0 on every task record** — the field exists but is not
   populated. Wall-clock above is derived from `run.log` timestamps.
6. **Not a determinism claim.** At the same seed and params, an earlier attempt
   on this same box drew a *different* T4.2 defect (correct format, wrong input
   grammar) than run 2.0's (feature dropped entirely). This run is one sample,
   not a reproducible fixed point.
7. **Single confound, not isolated.** This run differs from run 2.0 in more than
   one variable (envelope 2048→4096, `salvage_budget` 0→1, Python 3.11/3.12-era
   → 3.14, both-model co-residency). The envelope is the *leading* candidate for
   the P4/V3.1 flips but is **not** established — see the pre-registered A/B in
   `docs/design/deployment-reconciliation.md` Phase 1.

## Reproduction

```bash
# stack: Ollama on the cast's endpoint. (Conduit only if you want the live view —
# see the C2 verification below: a phased run needs no homeserver.)
agora doctor                                             # expect all green
python scripts/run_phased.py campaigns/integration-run-2.yaml --status   # P3..P9 pending
python scripts/run_phased.py campaigns/integration-run-2.yaml --next     # x6, one gate per invocation
python scripts/run_phased.py campaigns/integration-run-2.yaml --auto     # or: advance-while-green
```

Provenance for this run (untracked, scratchpad clone):
`runs_out/integration-run-2/` — `phases.jsonl`, `tasks.jsonl`, `run.log`.

---

## Addendum — C2 verification run (2026-07-15, same day, `--auto` + no Matrix)

A second run, executed to accept the C2 ruling (the Matrix surface is optional).
Same campaign, cast, flow and params; **only `output_dir` and `run.id` differ**
(F26: the delta is stated rather than implied). Fixture: **Docker entirely down**
— API dead, `conduit http=000`, the `docker-desktop` WSL distro terminated (the
container survives killing Docker Desktop's UI and keeps serving 6167, which is
worth knowing) — and a `.env` containing **only** `AGORA_OLLAMA_BASE_URL`. No
Matrix password existed anywhere.

**What it proved (the point of the run):**

```
[ OK ] ollama: reachable at http://localhost:11700 (version 0.31.1)
[ OK ] ollama-models: all 4 cast model(s) present
[SKIP] conduit: skipped (observer off)
all checks passed. (1 skipped)
[*] Matrix surface OFF (enable_observer=False) — no Conduit required.

matrix(null): create_room name='agent:impl'      -> !null-1
matrix(null): create_room name='agent:verifier'  -> !null-2
matrix(null): create_room name='project:echobot' -> !null-3
```

- **P3 GREEN, P4 GREEN with no homeserver in existence.** Python + Ollama is the
  whole dependency set for a phased run.
- **The per-tool null semantics fired in the wild — and the phase still passed.**
  V3.1 called `post_note` (`note (observer off): # P3 Scaffold Verification` →
  recorded to run.log) and `request_review` (→ LOUD `UNAVAILABLE`), then went on
  to pass P3's gate. Provenance unaffected (F3); only delivery disappeared.
- `[SKIP]` is a third state, not a green: nothing claimed a homeserver was
  verified when none was contacted.

**What it did NOT prove — acceptance (i) is unmet.** `--auto` **STOPPED at P5,
gate RED**, so the lifecycle did not reach P9 in one command. The harness
behaved exactly as designed (advance-while-green; stop on red; print the repair
command; repairs stay operator actions). The reds were **model** defects, and
C2 is exonerated by the evidence: **T5.1 called only `write_file` +
`mark_complete` — zero Matrix-touching tools** — so the null semantics never
entered the tester's context.

```
P5 RED  (1 fail / 7 pass)   test_unknown_command
        contract:  unknown !cmd -> "unknown command: cmd (try !help)"   (no bang)
        tester:    "unknown command: unknown_command (try !help)"       (no bang — correct)
        impl:      "unknown command: !bogus (try !help)"                (kept the bang — WRONG)
  -> repair 1/1: --rerun-task T4.1 --oracle P5   (cross-phase; T4.1 owns the router)
P5 RED  (2 fail / 6 pass)   -> STOP (second red on one gate; waivers forbidden)
        the bang WAS fixed: '!bogus' -> 'unknown command: bogus (try !help)'
        but !roll was DROPPED: '!roll 2d6' -> 'unknown command: roll (try !help)'
        grep -c roll core.py = 1 — the sole survivor is the help text, which still
        ADVERTISES a command the dispatcher no longer implements.
```

**F12×F14, reproduced live.** The measured (write-only) surface forces a
whole-file rewrite to change one string; gemma fixed the named defect and lost
an unrelated feature, leaving the artifact internally inconsistent — and P5's
suite caught it. This is the mirror image of run 2.0's P4 case ("T4.2 rewrote
core.py to add !roll and OMITTED roll entirely", Part 8) and reproduces run
2.0's **P5 trajectory almost exactly**: red → one T4.1 cross-phase repair → red
→ stop.

**Non-determinism, again, at a fixed seed.** The baseline's implementation
stripped the bang correctly (`'!bogus' -> 'unknown command: bogus (try !help)'`)
under *identical* seed and params. Two runs, same conditions, different defects.
Whatever else the baseline is, it is not a reproducible fixed point — the
caveat in "Deviations" above is now demonstrated rather than asserted.

Provenance: `<scratchpad>/runs_out/verify-c2/` (untracked).
