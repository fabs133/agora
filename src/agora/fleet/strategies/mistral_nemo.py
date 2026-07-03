"""``mistral_nemo`` strategy — fold trailing tool results into a user turn.

Mismatch (verified 2026-07-03, daemon 0.31.1): the Ollama mistral-nemo template
injects ``[AVAILABLE_TOOLS]`` only when a user message is within the last 2
messages, and the system prompt only when the user message IS the last message.
Agora emits tool results as role ``tool``, so mid-loop history is
``[user, assistant, tool, ...]`` — from the second generation onward the model
sees neither the manifest nor the system prompt. (Evidence: the trailing-``tool``
render is 198 chars with neither; appending a user turn jumps it to 763.)

Target: ``wrap_messages`` rewrites a trailing ``tool`` message to role ``user``
with the payload prefixed ``"Tool results:\\n"`` (markers preserved verbatim).
Both template conditions then fire every generation, re-establishing system +
manifest. ``wrap_system`` / ``wrap_tools`` are identity.

Accepted tradeoff: the folded results lose their ``[TOOL_RESULTS]`` control-token
framing. If the wrapper result is ambiguous (PARTIAL), the designated follow-up
is a corrected-template Modelfile variant, not a bigger wrapper.
"""

from __future__ import annotations

from typing import Any

from agora.fleet.strategies import Strategy

_PREFIX = "Tool results:\n"


class MistralNemoStrategy(Strategy):
    """Fold a trailing ``role: tool`` message into a ``role: user`` message."""

    name = "mistral_nemo"

    def wrap_messages(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not messages or messages[-1].get("role") != "tool":
            return messages
        last = messages[-1]
        folded = {"role": "user", "content": _PREFIX + (last.get("content", "") or "")}
        # Return a new list; never mutate the caller's message history.
        return [*messages[:-1], folded]
