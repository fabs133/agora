# The observation-channel series — consolidated findings (v5–v7 era)

*Written 2026-07-06 to close a claims-audit gap: the rendering/channel
arc was executed across campaigns (axis-1-v5/v6/v7-gemma) and chat-side
reports but never consolidated as a committed findings document. This
note is partly historical reconstruction; each item cites its committed
home where one exists, and marks itself as the committed home where one
did not. Note on versions: probe_version v4 was run under campaign
axis-1-v3.0.1 (3 runs); v5/v6/v7 are the gemma rendering-series
campaigns (5 runs each).*

## The claim this document pins

Across the program, the model's observation channel was found lying in
**six distinct ways** — each one caused content failures that were
initially attributable to the model, and each was removed behind a
single-variable rerun:

1. **Stale outputs + write-once guard.** Workspace `out/` never reset;
   the guard blocked live writes and disabled write_file for the task;
   predicates evaluated fossil files. Committed:
   `docs/runs/axis-1-v3/forensics-stale-out.md`; axis-1 v2 findings §8.
2. **Tool-result marker inside content.** `[read_file#N]` prefixed the
   content channel; faithful models copied it into artifacts;
   prefix-tolerant `contains` predicates masked it. Committed: axis-1
   v2 findings §8 (standing-defect entry); removal validated in the
   v5–v7 reruns (campaign YAMLs; provenance under runs_out/).
3. **Write-path newline translation.** Python text-mode `write_text`
   turned `\n` into CRLF on Windows across tools, predicates, and
   capture — byte-equality tasks partly measured the translation layer.
   Committed: `docs/runs/determinism-probe/findings.md` §5; fixed in
   probe v5 (byte-IO discipline).
4. **Low-salience byte transmission.** The deciding byte for the
   equality task (a trailing newline at a decorated message boundary)
   was transmitted at minimum salience, creating the near-tie that
   FP-level jitter then decided. Committed: determinism-probe findings
   (mechanism, predictions P1–P3, falsifier); v5 collapsed the modes.
5. **Daemon rendering-branch re-encoding (v6).** Adding protocol-level
   tool_call_id/tool_name switched the daemon's gemma renderer onto a
   structured branch that re-encoded content (observed as escaped
   newlines faithfully copied by the model); the v6→v7 inversion
   (copies escaped, compose real) was the diagnostic. Committed home:
   THIS document (evidence: axis-1-v6/v7-gemma campaign YAMLs,
   session-era reports, runs_out provenance).
6. **Seed-fixture newline translation.** `seed_probe_files` still used
   `write_text`, leaving CRLF seeds that failed byte-correct LF output
   (run 2.1 era). Committed home: THIS document (fix commit in the
   integration-run-2 prep series).

## What v7 established

With all six removed, the channel is byte-transparent end-to-end:
content is delivered verbatim, one message per result, no markers, no
re-encoding, byte-exact IO in tools/predicates/capture/seeds. The
program's first genuine 9/9 (gemma, production harness, probe v7) and
the run-2/run-3 lifecycle results all rest on this state. Doctrine, as
carried into the arc document: models copy what they observe with high
fidelity; every content failure in this program traced to the
observation channel until the channel was proven transparent — only
then did the two genuine model boundaries (F14, F18‴) become visible
and measurable.

## Audit note

Items 1–4 are independently committed as cited; items 5–6 were, until
this note, attested only by campaign artifacts and session reports.
Claims in `docs/arc/arc.md` §2 cite this document.
