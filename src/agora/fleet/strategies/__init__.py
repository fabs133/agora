"""Per-model prompting strategies (axis-1 v2, Phase 3).

A *strategy* is a small, model-specific transform applied to the three inputs
of one ``complete()`` call — the system prompt, the tool manifest, and the
message history — to compensate for a documented prompting mismatch (see
``docs/research/prompting-strategies.md``). Strategies never touch tool
*execution* or the assistant-turn / tool-result *shaping*; those pass through
untouched.

The mechanism is a **wrapper**, never a modification of the underlying adapter:
:class:`StrategyAdapter` composes over any :class:`~agora.fleet.llm_adapter.LLMProtocol`
implementation and applies the strategy's three ``wrap_*`` hooks around
``complete``. The base :class:`Strategy` is identity on all three, so a
strategy overrides only the hook its mismatch requires.

Selection travels by name (campaign YAML → ``AGORA_STRATEGY`` env → runner),
mirroring the ``AGORA_ARM_*`` pattern. ``strategy is None`` means *no wrapper is
constructed at all* — the null path is byte-identical to axis-1 v1 by
construction, not by proof.
"""

from __future__ import annotations

from typing import Any

from agora.fleet.llm_adapter import LLMProtocol, LLMResponse, ToolCall


class Strategy:
    """Base strategy: identity on all three wraps.

    Concrete strategies subclass and override only the hook their documented
    mismatch requires; the other two stay identity.
    """

    #: Registry key. Set on each concrete subclass.
    name: str = ""

    def wrap_system(self, system: str) -> str:
        """Transform the system prompt before it reaches the model."""
        return system

    def wrap_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Transform the tool manifest before it reaches the model."""
        return tools

    def wrap_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Transform the message history before it reaches the model."""
        return messages


class StrategyAdapter:
    """Wrap an inner :class:`LLMProtocol`, applying ``strategy`` around ``complete``.

    The three ``wrap_*`` hooks fire on the system / tools / messages passed to
    ``complete``; ``format_assistant_turn`` and ``format_tool_results`` are
    passed straight through so the wire shaping is unchanged. This is a
    composition, not a subclass — the inner adapter is untouched.
    """

    def __init__(self, inner: LLMProtocol, strategy: Strategy) -> None:
        self._inner = inner
        self._strategy = strategy

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        max_tokens: int | None = None,
    ) -> LLMResponse:
        wrapped_tools = (
            self._strategy.wrap_tools(tools) if tools is not None else None
        )
        return await self._inner.complete(
            self._strategy.wrap_messages(messages),
            system=self._strategy.wrap_system(system),
            tools=wrapped_tools,
            model=model,
            max_tokens=max_tokens,
        )

    def format_assistant_turn(self, response: LLMResponse) -> dict[str, Any]:
        return self._inner.format_assistant_turn(response)

    def format_tool_results(
        self, calls: list[ToolCall], results: list[str]
    ) -> dict[str, Any]:
        return self._inner.format_tool_results(calls, results)


# --------------------------------------------------------------------- registry

from agora.fleet.strategies.mistral_nemo import MistralNemoStrategy  # noqa: E402
from agora.fleet.strategies.qwen2_5_coder import Qwen25CoderStrategy  # noqa: E402

#: Every selectable strategy, keyed by the name used in campaign YAML and the
#: ``AGORA_STRATEGY`` env var. ``load_campaign`` validates run.strategy against
#: these keys, so a typo fails at load, not at run 23 of 40.
STRATEGIES: dict[str, Strategy] = {
    s.name: s
    for s in (Qwen25CoderStrategy(), MistralNemoStrategy())
}


def resolve(name: str | None) -> Strategy | None:
    """Resolve a strategy name to its instance.

    ``None`` or empty ⇒ ``None`` (no strategy; caller constructs no wrapper).
    An unknown non-empty name raises :class:`KeyError` — callers that accept
    user input should validate against :data:`STRATEGIES` first (``load_campaign``
    does), so reaching here with a bad name is a programming error worth failing
    loudly on.
    """
    if not name:
        return None
    try:
        return STRATEGIES[name]
    except KeyError:
        raise KeyError(
            f"unknown strategy {name!r}; known: {sorted(STRATEGIES)}"
        ) from None


__all__ = ["Strategy", "StrategyAdapter", "STRATEGIES", "resolve"]
