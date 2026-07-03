"""Strategy registry + StrategyAdapter wrapper (axis-1 v2, Phase 3)."""

from __future__ import annotations

from typing import Any

import pytest

from agora.fleet.llm_adapter import LLMResponse, ToolCall
from agora.fleet.strategies import (
    STRATEGIES,
    Strategy,
    StrategyAdapter,
    resolve,
)


def test_resolve_none_builds_no_wrapper() -> None:
    """The control path: no strategy name ⇒ resolve None ⇒ caller wraps nothing.

    Identity-by-construction — the byte-identical-to-v1 guarantee holds because
    ``None``/empty never yields a Strategy, so no StrategyAdapter is built.
    """
    assert resolve(None) is None
    assert resolve("") is None
    # A registered name resolves to its instance; an unknown one fails loudly.
    assert isinstance(resolve("qwen2_5_coder"), Strategy)
    assert set(STRATEGIES) == {"qwen2_5_coder", "mistral_nemo"}
    with pytest.raises(KeyError):
        resolve("nope")


class _StubInner:
    """Records exactly what ``complete`` received; identity shaping."""

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.seen = {"messages": messages, "system": system, "tools": tools}
        return LLMResponse(content="ok")

    def format_assistant_turn(self, response: LLMResponse) -> dict[str, Any]:
        return {"role": "assistant", "content": response.content}

    def format_tool_results(
        self, calls: list[ToolCall], results: list[str]
    ) -> dict[str, Any]:
        return {"role": "tool", "content": "|".join(results)}


class _StubStrategy(Strategy):
    """Tags all three inputs so the wrapper's plumbing is observable."""

    name = "_stub"

    def wrap_system(self, system: str) -> str:
        return system + " [S]"

    def wrap_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [*tools, {"name": "_injected"}]

    def wrap_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [*messages, {"role": "user", "content": "[M]"}]


async def test_strategy_adapter_wraps_system_tools_messages() -> None:
    """complete() must receive the strategy-transformed system/tools/messages,
    while format_* pass straight through to the inner adapter."""
    inner = _StubInner()
    adapter = StrategyAdapter(inner, _StubStrategy())

    await adapter.complete(
        [{"role": "user", "content": "hi"}],
        system="base",
        tools=[{"name": "read_file"}],
    )

    assert inner.seen["system"] == "base [S]"
    assert {"name": "_injected"} in inner.seen["tools"]
    assert inner.seen["messages"][-1] == {"role": "user", "content": "[M]"}
    # Shaping is delegated untouched.
    assert adapter.format_tool_results(
        [ToolCall(id="0", name="read_file", arguments={})], ["r"]
    ) == {"role": "tool", "content": "r"}


async def test_strategy_adapter_passes_none_tools_through() -> None:
    """A None tool manifest is forwarded as None (no wrap on the empty case)."""
    inner = _StubInner()
    adapter = StrategyAdapter(inner, _StubStrategy())
    await adapter.complete([{"role": "user", "content": "hi"}], system="s", tools=None)
    assert inner.seen["tools"] is None


def test_mistral_nemo_folds_trailing_tool_message() -> None:
    """Trailing role=tool → role=user with the 'Tool results:' prefix; the
    payload (including [name#id] markers) is preserved and the input untouched."""
    strat = resolve("mistral_nemo")
    messages = [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file"}}]},
        {"role": "tool", "content": "[read_file#0] alpha", "tool_name": "read_file"},
    ]
    out = strat.wrap_messages(messages)
    assert out[-1] == {"role": "user", "content": "Tool results:\n[read_file#0] alpha"}
    assert messages[-1]["role"] == "tool"  # original not mutated
    # No trailing tool message ⇒ identity.
    plain = [{"role": "user", "content": "hi"}]
    assert strat.wrap_messages(plain) == plain


def test_qwen_coder_appends_fewshot_to_system() -> None:
    strat = resolve("qwen2_5_coder")
    out = strat.wrap_system("You are an implementer.")
    assert out.startswith("You are an implementer.")
    assert "read_file" in out and "write_file" in out
    assert "one tool call per turn" in out
