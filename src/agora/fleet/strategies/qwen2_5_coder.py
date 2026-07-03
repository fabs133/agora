"""``qwen2_5_coder`` strategy — few-shot the text channel (Phase 2 design).

Mismatch: qwen2.5-coder wasn't trained on tool-call tokens; under the default
protocol it narrates intent instead of sustaining calls (~0.67 calls/task in
v1, all via text-fallback extraction, 0/18 pass). The failure is loop
breakdown, not parsing — Agora's text-fallback parser already extracts the
``json`` blocks the model does emit.

Target: ``wrap_system`` appends two complete worked examples of tool-call turns
in the exact fenced-JSON shape the text-fallback parser consumes (one
``read_file``, one ``write_file``, realistic arguments), followed by two rules.
``wrap_tools`` / ``wrap_messages`` are identity. This is a text-channel
strategy: it is judged on ``pass_rate`` / ``tool_calls_total``;
``structured_emission_rate`` may stay 0 by design.
"""

from __future__ import annotations

from agora.fleet.strategies import Strategy

#: Appended to the system prompt. The two examples use the exact
#: ```json {"name": ..., "arguments": {...}} ``` shape
#: ``_parse_tool_calls_from_text`` extracts, so a model that mimics them emits
#: calls the runtime executes. Rules target the two v1 failure modes: too few
#: calls per turn, and narrating an action instead of calling it.
_FEWSHOT = """

# Tool-calling format

Emit each tool call as a single fenced JSON block, nothing else in the turn:

```json
{"name": "read_file", "arguments": {"path": "plan/seed_a.txt"}}
```

```json
{"name": "write_file", "arguments": {"path": "out/result.txt", "content": "alpha\\nbeta\\n"}}
```

Rules:
1. Emit exactly one tool call per turn, as the fenced JSON block above.
2. Never describe an action in prose without emitting the corresponding tool
   call — if you would say "now I read the file", emit the read_file call
   instead."""


class Qwen25CoderStrategy(Strategy):
    """Append a fenced-JSON few-shot block + two rules to the system prompt."""

    name = "qwen2_5_coder"

    def wrap_system(self, system: str) -> str:
        return (system or "") + _FEWSHOT
