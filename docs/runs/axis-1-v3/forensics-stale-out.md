# Stale-`out/` forensics — live vs stale-backed task outcomes

Read-only reclassification of equality task-cells across campaigns. See `scripts/forensic_stale_out.py` for the exact rules.

## Per-model × per-campaign classification

### `axis-1-tool-call-fidelity`

| model | live_pass | stale_backed_pass | guard_artifact_fail | genuine_fail |
|---|---|---|---|---|
| gemma-e4b | 0 | 12 | 6 | 0 |
| mistral-nemo-12b | 0 | 12 | 0 | 6 |
| qwen-coder-14b | 0 | 0 | 0 | 18 |
| qwen-coder-7b | 0 | 0 | 0 | 18 |
| qwen-instruct-7b | 2 | 10 | 0 | 6 |
| qwen3-30b | 0 | 12 | 0 | 6 |
| **TOTAL** | **2** | **46** | **6** | **54** |

### `axis-1-tool-call-fidelity-v2`

| model | live_pass | stale_backed_pass | guard_artifact_fail | genuine_fail |
|---|---|---|---|---|
| gemma-e4b | 0 | 12 | 6 | 0 |
| mistral-nemo-12b | 0 | 12 | 0 | 6 |
| qwen-coder-14b | 0 | 12 | 0 | 6 |
| qwen-coder-7b | 0 | 12 | 0 | 6 |
| qwen-instruct-7b | 0 | 12 | 0 | 6 |
| qwen3-30b | 0 | 20 | 0 | 10 |
| **TOTAL** | **0** | **80** | **6** | **34** |

### `axis-1-v3.0`

| model | live_pass | stale_backed_pass | guard_artifact_fail | genuine_fail |
|---|---|---|---|---|
| gemma-e4b | 0 | 6 | 3 | 0 |
| mistral-nemo-12b | 0 | 6 | 0 | 3 |
| qwen-coder-14b | 0 | 6 | 0 | 3 |
| qwen-instruct-7b | 0 | 6 | 0 | 3 |
| qwen3-30b | 0 | 6 | 0 | 3 |
| **TOTAL** | **0** | **30** | **3** | **12** |

## Appendix — verbatim attempted-write content (exhibits)

**S4 diagnosis — gemma `loop_depth` attempted content** (`axis-1-tool-call-fidelity/r019`, write guard_blocked, classified guard_artifact_fail). The newline-join is byte-correct — the failure is the guard block against a stale file, not the model:

```
'apple\napricot\navocado\nblueberry\nblackberry\nboysenberry\n'
```

**Copy-safety exhibit — gemma `small_chain` attempted content** (`axis-1-tool-call-fidelity/r019`). The `[read_file#0]` tool-result marker leaked verbatim into the copied output (integration-blocking defect, logged; not fixed here):

```
'[read_file#0] alpha line one\nbeta line two\ngamma line three\ndelta line four\n'
```

