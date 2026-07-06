# Final-turn & mark_complete autopsy — `axis-1-tool-call-fidelity-v2`

Read-only classification of failed task-cells. No inference; see the script docstring for the exact rules.

## Final assistant turn on failed cells

| model | failed cells | empty | prose_no_call | malformed_call | max_iter |
|---|---|---|---|---|---|
| gemma-e4b | 6 | 0 | 6 | 0 | 0 |
| mistral-nemo-12b | 18 | 15 | 3 | 0 | 0 |
| qwen-coder-14b | 18 | 0 | 18 | 0 | 0 |
| qwen-coder-7b | 18 | 0 | 18 | 0 | 0 |
| qwen-instruct-7b | 18 | 18 | 0 | 0 | 0 |
| qwen3-30b | 23 | 23 | 0 | 0 | 0 |
| **TOTAL** | **101** | **56** | **45** | **0** | **0** |

## mark_complete argument patterns (all invocations)

| model | calls | summary_ok | write_file_args | other_malformed |
|---|---|---|---|---|
| gemma-e4b | 18 | 18 | 0 | 0 |
| mistral-nemo-12b | 0 | 0 | 0 | 0 |
| qwen-coder-14b | 0 | 0 | 0 | 0 |
| qwen-coder-7b | 0 | 0 | 0 | 0 |
| qwen-instruct-7b | 6 | 0 | 6 | 0 |
| qwen3-30b | 18 | 11 | 7 | 0 |
| **TOTAL** | **42** | **29** | **13** | **0** |

