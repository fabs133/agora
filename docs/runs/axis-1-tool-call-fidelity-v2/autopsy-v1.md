# Final-turn & mark_complete autopsy — `axis-1-tool-call-fidelity`

Read-only classification of failed task-cells. No inference; see the script docstring for the exact rules.

## Final assistant turn on failed cells

| model | failed cells | empty | prose_no_call | malformed_call | max_iter |
|---|---|---|---|---|---|
| gemma-e4b | 6 | 0 | 6 | 0 | 0 |
| mistral-nemo-12b | 18 | 12 | 6 | 0 | 0 |
| qwen-coder-14b | 18 | 0 | 18 | 0 | 0 |
| qwen-coder-7b | 18 | 0 | 18 | 0 | 0 |
| qwen-instruct-7b | 16 | 15 | 0 | 0 | 1 |
| qwen3-30b | 15 | 15 | 0 | 0 | 0 |
| **TOTAL** | **91** | **42** | **48** | **0** | **1** |

## mark_complete argument patterns (all invocations)

| model | calls | summary_ok | write_file_args | other_malformed |
|---|---|---|---|---|
| gemma-e4b | 18 | 18 | 0 | 0 |
| mistral-nemo-12b | 0 | 0 | 0 | 0 |
| qwen-coder-14b | 0 | 0 | 0 | 0 |
| qwen-coder-7b | 0 | 0 | 0 | 0 |
| qwen-instruct-7b | 12 | 12 | 0 | 0 |
| qwen3-30b | 12 | 3 | 9 | 0 |
| **TOTAL** | **42** | **33** | **9** | **0** |

