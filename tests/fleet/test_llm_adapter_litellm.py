"""LiteLLMAdapter: multi-provider adapter backed by litellm.

Tests cover:
  - factory routing for provider-prefixed model strings
  - ``format_assistant_turn`` produces OpenAI-shape tool_calls
  - ``format_tool_results`` returns ONE message per tool_call_id (list)
  - tool schema translation (Agora/Anthropic ``input_schema`` → OpenAI
    ``function.parameters``)
  - response parsing (content + tool_calls with JSON-string arguments)
  - ``_append_turn`` utility bridges dict + list return shapes

``litellm`` is a heavy optional dep; most tests stub ``sys.modules['litellm']``
so the adapter constructs and calls complete() against a fake module without
the real package installed.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from agora.core.errors import AgoraError
from agora.fleet.llm_adapter import (
    LITELLM_PROVIDER_PREFIXES,
    LiteLLMAdapter,
    LLMResponse,
    ToolCall,
    _is_litellm_model,
    create_llm_adapter,
)

# ------------------------------------------------------------ litellm stub


class _FakeLiteLLMModule:
    """Minimal stand-in for the ``litellm`` package.

    Exposes the one entry point the adapter uses (``acompletion``) plus
    the bare module-level attrs the adapter's import path touches. Tests
    overwrite ``acompletion_impl`` to control return values.
    """

    def __init__(self):
        self.acompletion_calls: list[dict[str, Any]] = []
        self.acompletion_impl = self._default_impl

    async def _default_impl(self, **kwargs: Any):
        """Default: echo the last user message as assistant content."""
        self.acompletion_calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ack", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

    async def acompletion(self, **kwargs: Any):
        self.acompletion_calls.append(kwargs)
        return await self.acompletion_impl(**kwargs)


@pytest.fixture
def fake_litellm(monkeypatch: pytest.MonkeyPatch) -> _FakeLiteLLMModule:
    """Install a fake ``litellm`` module into ``sys.modules`` for the test."""
    fake = _FakeLiteLLMModule()
    # Override the default implementation so ``acompletion_calls`` records
    # args passed directly (not via _default_impl's indirection).
    original_acompletion = fake.acompletion

    async def _tracked(**kwargs: Any):
        # _default_impl already appends; strip duplicate by resetting here.
        return await original_acompletion(**kwargs)

    monkeypatch.setitem(sys.modules, "litellm", fake)  # type: ignore[arg-type]
    return fake


# --------------------------------------------------------- factory routing


def test_is_litellm_model_recognises_provider_prefixes():
    for prefix in ("openai/", "anthropic/", "gemini/", "mistral/", "groq/"):
        assert _is_litellm_model(f"{prefix}some-model") is True


def test_is_litellm_model_explicit_litellm_override():
    assert _is_litellm_model("litellm/custom_provider/custom-model") is True


def test_is_litellm_model_rejects_non_provider_strings():
    assert _is_litellm_model("ollama/qwen2.5:7b") is False
    assert _is_litellm_model("claude-sonnet-4-20250514") is False
    assert _is_litellm_model("claude-code/subscription") is False
    assert _is_litellm_model("gpt-4") is False  # bare OpenAI name without prefix
    assert _is_litellm_model("") is False


def test_factory_routes_openai_to_litellm(fake_litellm):
    adapter = create_llm_adapter("openai/gpt-4o-mini")
    assert isinstance(adapter, LiteLLMAdapter)


def test_factory_routes_anthropic_prefix_to_litellm(fake_litellm):
    """anthropic/claude-* routes to LiteLLM, NOT the direct AnthropicAdapter
    (which uses the bare claude-* form)."""
    adapter = create_llm_adapter("anthropic/claude-haiku-4-5")
    assert isinstance(adapter, LiteLLMAdapter)


def test_factory_routes_gemini_to_litellm(fake_litellm):
    adapter = create_llm_adapter("gemini/gemini-1.5-flash")
    assert isinstance(adapter, LiteLLMAdapter)


def test_factory_routes_litellm_explicit_override_to_litellm(fake_litellm):
    adapter = create_llm_adapter("litellm/some_new_provider/model-x")
    assert isinstance(adapter, LiteLLMAdapter)


def test_factory_direct_claude_still_routes_to_anthropic_adapter():
    """Regression guard: bare ``claude-*`` keeps the direct Anthropic path
    (requires api_key), not LiteLLM."""
    pytest.importorskip("anthropic")
    from agora.fleet.llm_adapter import AnthropicAdapter

    adapter = create_llm_adapter("claude-sonnet-4-20250514", api_key="sk-test")
    assert isinstance(adapter, AnthropicAdapter)


def test_factory_unknown_model_still_raises(fake_litellm):
    """Non-prefixed unknown names still raise — no silent misroute."""
    with pytest.raises(AgoraError, match="no adapter"):
        create_llm_adapter("gpt-4")


def test_adapter_missing_litellm_raises_helpful_error(monkeypatch):
    """When litellm isn't importable, the constructor raises with a pip hint."""
    # Ensure there's no cached module.
    monkeypatch.delitem(sys.modules, "litellm", raising=False)
    # Prevent actual import by making import machinery fail.
    import builtins

    _original_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "litellm":
            raise ImportError("No module named 'litellm'")
        return _original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    with pytest.raises(AgoraError, match="litellm"):
        LiteLLMAdapter()


# --------------------------------------------------------- assistant turn


def test_format_assistant_turn_text_only(fake_litellm):
    adapter = LiteLLMAdapter()
    turn = adapter.format_assistant_turn(LLMResponse(content="hello world"))
    assert turn == {"role": "assistant", "content": "hello world"}
    assert "tool_calls" not in turn


def test_format_assistant_turn_with_tool_calls(fake_litellm):
    adapter = LiteLLMAdapter()
    turn = adapter.format_assistant_turn(
        LLMResponse(
            content="",
            tool_calls=(
                ToolCall(
                    id="call_abc",
                    name="read_file",
                    arguments={"path": "README.md"},
                ),
            ),
        )
    )
    assert turn["role"] == "assistant"
    assert turn["content"] == ""
    tcs = turn["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "call_abc"
    assert tcs[0]["type"] == "function"
    assert tcs[0]["function"]["name"] == "read_file"
    # Arguments is a JSON STRING per OpenAI spec, not a dict.
    assert tcs[0]["function"]["arguments"] == json.dumps({"path": "README.md"})


def test_format_assistant_turn_multiple_tool_calls(fake_litellm):
    adapter = LiteLLMAdapter()
    turn = adapter.format_assistant_turn(
        LLMResponse(
            content="",
            tool_calls=(
                ToolCall(id="c1", name="read_file", arguments={"path": "a"}),
                ToolCall(id="c2", name="write_file", arguments={"path": "b", "content": "x"}),
            ),
        )
    )
    assert len(turn["tool_calls"]) == 2
    assert turn["tool_calls"][0]["id"] == "c1"
    assert turn["tool_calls"][1]["id"] == "c2"


# --------------------------------------------------------- tool results


def test_format_tool_results_returns_list(fake_litellm):
    """Unlike Anthropic/Ollama which fold multiple results into one dict,
    LiteLLM must produce ONE message per tool_call_id so strict OpenAI
    validates the reply."""
    adapter = LiteLLMAdapter()
    calls = [
        ToolCall(id="c1", name="read_file", arguments={}),
        ToolCall(id="c2", name="write_file", arguments={}),
    ]
    results = ["file content", "wrote 42 bytes"]
    turn = adapter.format_tool_results(calls, results)
    assert isinstance(turn, list)
    assert len(turn) == 2
    assert turn[0] == {"role": "tool", "tool_call_id": "c1", "content": "file content"}
    assert turn[1] == {"role": "tool", "tool_call_id": "c2", "content": "wrote 42 bytes"}


def test_format_tool_results_single_call(fake_litellm):
    """Single-call case still returns a list of one — consistent shape."""
    adapter = LiteLLMAdapter()
    turn = adapter.format_tool_results(
        [ToolCall(id="c1", name="f", arguments={})],
        ["ok"],
    )
    assert isinstance(turn, list)
    assert len(turn) == 1


# --------------------------------------------------------- tool schema


def test_tool_to_openai_shape_converts_anthropic_schema():
    """Agora stores tool definitions with Anthropic's ``input_schema`` key;
    OpenAI expects ``function.parameters``."""
    agora_tool = {
        "name": "read_file",
        "description": "Read a file from the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
    openai_tool = LiteLLMAdapter._tool_to_openai_shape(agora_tool)
    assert openai_tool["type"] == "function"
    assert openai_tool["function"]["name"] == "read_file"
    assert openai_tool["function"]["description"] == "Read a file from the workspace."
    assert openai_tool["function"]["parameters"] == agora_tool["input_schema"]


def test_tool_to_openai_shape_falls_back_to_parameters_key():
    """If a tool is already in OpenAI shape (has ``parameters``), don't
    double-convert."""
    already_openai = {
        "name": "x",
        "description": "",
        "parameters": {"type": "object", "properties": {}},
    }
    out = LiteLLMAdapter._tool_to_openai_shape(already_openai)
    assert out["function"]["parameters"] == already_openai["parameters"]


def test_tool_to_openai_shape_empty_schema_is_harmless():
    out = LiteLLMAdapter._tool_to_openai_shape({"name": "x"})
    assert out["function"]["parameters"] == {}


# --------------------------------------------------------- response parsing


def _fake_response(
    content: str = "",
    tool_calls: list[SimpleNamespace] | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> SimpleNamespace:
    """Build a LiteLLM-shaped response object from the fields a test needs."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
    )


def test_parse_response_content_only():
    resp = _fake_response(content="hello world")
    parsed = LiteLLMAdapter._parse_response(resp)
    assert parsed.content == "hello world"
    assert parsed.tool_calls == ()
    assert parsed.usage == {"input_tokens": 10, "output_tokens": 5}
    assert parsed.stop_reason == "stop"


def test_parse_response_tool_calls_json_string_arguments():
    """LiteLLM/OpenAI ships tool arguments as a JSON string. Parser must
    deserialise it into a dict."""
    tc = SimpleNamespace(
        id="call_001",
        function=SimpleNamespace(
            name="read_file",
            arguments='{"path": "README.md"}',
        ),
    )
    resp = _fake_response(content="", tool_calls=[tc], finish_reason="tool_calls")
    parsed = LiteLLMAdapter._parse_response(resp)
    assert len(parsed.tool_calls) == 1
    call = parsed.tool_calls[0]
    assert call.id == "call_001"
    assert call.name == "read_file"
    assert call.arguments == {"path": "README.md"}


def test_parse_response_tool_calls_already_dict_arguments():
    """Some providers return ``arguments`` as a dict already; tolerate it."""
    tc = SimpleNamespace(
        id="call_002",
        function=SimpleNamespace(name="f", arguments={"x": 1}),
    )
    resp = _fake_response(tool_calls=[tc])
    parsed = LiteLLMAdapter._parse_response(resp)
    assert parsed.tool_calls[0].arguments == {"x": 1}


def test_parse_response_tool_calls_malformed_json_preserves_raw():
    """If the model emits invalid JSON in arguments, preserve it under ``_raw``
    so the tool call still fires (the downstream executor can diagnose)."""
    tc = SimpleNamespace(
        id="call_003",
        function=SimpleNamespace(name="f", arguments="{not valid json}"),
    )
    resp = _fake_response(tool_calls=[tc])
    parsed = LiteLLMAdapter._parse_response(resp)
    assert parsed.tool_calls[0].arguments == {"_raw": "{not valid json}"}


def test_parse_response_tool_calls_empty_arguments():
    tc = SimpleNamespace(
        id="call_004",
        function=SimpleNamespace(name="mark_complete", arguments=""),
    )
    resp = _fake_response(tool_calls=[tc])
    parsed = LiteLLMAdapter._parse_response(resp)
    assert parsed.tool_calls[0].arguments == {}


def test_parse_response_usage_input_output_fallback():
    """Some LiteLLM providers report ``input_tokens`` / ``output_tokens``
    instead of ``prompt_tokens`` / ``completion_tokens``. Handle both."""
    resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="x", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(input_tokens=7, output_tokens=3),
    )
    parsed = LiteLLMAdapter._parse_response(resp)
    assert parsed.usage == {"input_tokens": 7, "output_tokens": 3}


def test_parse_response_missing_choices_raises():
    resp = SimpleNamespace(choices=[])
    with pytest.raises(AgoraError, match="unparseable"):
        LiteLLMAdapter._parse_response(resp)


# --------------------------------------------------------- end-to-end complete


async def test_complete_sends_openai_shape_messages(fake_litellm):
    adapter = LiteLLMAdapter(default_model="openai/gpt-4o-mini")
    await adapter.complete(
        messages=[{"role": "user", "content": "hi"}],
        system="you are helpful",
        tools=[
            {
                "name": "read_file",
                "description": "Read",
                "input_schema": {"type": "object"},
            }
        ],
    )
    assert fake_litellm.acompletion_calls, "acompletion was not invoked"
    call = fake_litellm.acompletion_calls[-1]
    assert call["model"] == "openai/gpt-4o-mini"
    # system becomes the FIRST role:system message, not a kwarg
    msgs = call["messages"]
    assert msgs[0] == {"role": "system", "content": "you are helpful"}
    assert msgs[1] == {"role": "user", "content": "hi"}
    # tools got translated to OpenAI shape
    assert call["tools"][0]["type"] == "function"
    assert call["tools"][0]["function"]["name"] == "read_file"
    assert call["tool_choice"] == "auto"


async def test_complete_strips_litellm_prefix_from_model(fake_litellm):
    adapter = LiteLLMAdapter(default_model="litellm/openai/gpt-4o-mini")
    await adapter.complete(messages=[{"role": "user", "content": "hi"}])
    call = fake_litellm.acompletion_calls[-1]
    assert call["model"] == "openai/gpt-4o-mini"


async def test_complete_uses_explicit_model_over_default(fake_litellm):
    adapter = LiteLLMAdapter(default_model="openai/gpt-4o-mini")
    await adapter.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="anthropic/claude-haiku-4-5",
    )
    call = fake_litellm.acompletion_calls[-1]
    assert call["model"] == "anthropic/claude-haiku-4-5"


async def test_complete_without_model_raises(fake_litellm):
    """Empty model + no default → raise loudly instead of calling LiteLLM."""
    adapter = LiteLLMAdapter()
    with pytest.raises(AgoraError, match="no model"):
        await adapter.complete(messages=[{"role": "user", "content": "hi"}])


async def test_complete_wraps_provider_errors_as_agoraerror(fake_litellm):
    """Provider errors (HTTP failures, auth, etc.) should bubble up as
    AgoraError with a readable context string, not raw provider types."""

    async def _fail(**kwargs):
        raise RuntimeError("simulated provider auth failure")

    fake_litellm.acompletion_impl = _fail
    adapter = LiteLLMAdapter(default_model="openai/gpt-4o-mini")
    with pytest.raises(AgoraError, match="openai/gpt-4o-mini"):
        await adapter.complete(messages=[{"role": "user", "content": "hi"}])


async def test_complete_returns_parsed_llm_response(fake_litellm):
    async def _impl(**kwargs):
        return _fake_response(content="done", finish_reason="stop")

    fake_litellm.acompletion_impl = _impl
    adapter = LiteLLMAdapter(default_model="openai/gpt-4o-mini")
    resp = await adapter.complete(messages=[{"role": "user", "content": "hi"}])
    assert isinstance(resp, LLMResponse)
    assert resp.content == "done"
    assert resp.stop_reason == "stop"


# --------------------------------------------------------- _append_turn util


def test_append_turn_appends_dict():
    from agora.fleet.agent_runtime import _append_turn

    messages: list[dict[str, Any]] = []
    _append_turn(messages, {"role": "user", "content": "hi"})
    assert messages == [{"role": "user", "content": "hi"}]


def test_append_turn_extends_list():
    from agora.fleet.agent_runtime import _append_turn

    messages: list[dict[str, Any]] = [{"role": "user", "content": "start"}]
    _append_turn(
        messages,
        [
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        ],
    )
    assert len(messages) == 3
    assert messages[1]["tool_call_id"] == "c1"
    assert messages[2]["tool_call_id"] == "c2"


def test_append_turn_empty_list_is_noop():
    from agora.fleet.agent_runtime import _append_turn

    messages: list[dict[str, Any]] = [{"role": "user", "content": "a"}]
    _append_turn(messages, [])
    assert len(messages) == 1


# --------------------------------------------------------- provider prefix set


def test_litellm_provider_prefixes_is_nonempty():
    """Smoke check — the prefix list is the single source of truth for
    factory routing; losing a common prefix silently re-routes users away
    from LiteLLM."""
    assert "openai/" in LITELLM_PROVIDER_PREFIXES
    assert "anthropic/" in LITELLM_PROVIDER_PREFIXES
    assert "gemini/" in LITELLM_PROVIDER_PREFIXES
    # At minimum three major providers.
    assert len(LITELLM_PROVIDER_PREFIXES) >= 3
