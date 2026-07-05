import pytest

from agora.core.errors import AgoraError
from agora.fleet.llm_adapter import (
    AnthropicAdapter,
    LLMProtocol,
    LLMResponse,
    OllamaAdapter,
    ToolCall,
    create_llm_adapter,
)


def test_create_adapter_routes_to_anthropic() -> None:
    pytest.importorskip("anthropic")
    adapter = create_llm_adapter("claude-sonnet-4", api_key="sk-test")
    assert isinstance(adapter, AnthropicAdapter)


def test_create_adapter_routes_to_ollama() -> None:
    adapter = create_llm_adapter("ollama/llama3.1")
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.base_url == "http://localhost:11434"


def test_create_adapter_forwards_num_ctx_and_max_concurrent() -> None:
    """create_llm_adapter must pipe num_ctx + max_concurrent to OllamaAdapter.

    Without this, a caller setting num_ctx in the factory silently fell back
    to the adapter's 16384 default — confirmed latent bug before the profile
    layer landed.
    """
    adapter = create_llm_adapter(
        "ollama/llama3.1",
        num_ctx=32_768,
        max_concurrent=2,
    )
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.num_ctx == 32_768
    assert adapter.max_concurrent == 2


def test_create_adapter_ollama_defaults_preserved_when_kwargs_omitted() -> None:
    adapter = create_llm_adapter("ollama/llama3.1")
    assert isinstance(adapter, OllamaAdapter)
    # Defaults match the pre-fix behaviour exactly.
    assert adapter.num_ctx == 16384
    assert adapter.max_concurrent == 1


def test_create_adapter_ollama_accepts_num_ctx_none() -> None:
    adapter = create_llm_adapter("ollama/llama3.1", num_ctx=None)
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.num_ctx is None


def test_create_adapter_unknown_model_raises() -> None:
    with pytest.raises(AgoraError, match="no adapter"):
        create_llm_adapter("gpt-4")


def test_create_adapter_anthropic_without_key_raises() -> None:
    # Missing-key guard is checked before the import happens; error message
    # points users at the Ollama / subprocess paths.
    with pytest.raises(AgoraError, match="api_key|subscription|ollama"):
        create_llm_adapter("claude-sonnet-4")


def test_create_adapter_routes_to_claude_code_subprocess(monkeypatch) -> None:
    from agora.fleet.claude_code_adapter import ClaudeCodeSubprocessAdapter

    monkeypatch.setattr("shutil.which", lambda _b: "/fake/path/claude")
    adapter = create_llm_adapter(
        "claude-code/subscription", binary="claude", allow=True
    )
    assert isinstance(adapter, ClaudeCodeSubprocessAdapter)


def test_ollama_adapter_strips_prefix_in_payload(monkeypatch) -> None:
    """Model payload sent to Ollama must not include the 'ollama/' prefix."""
    import asyncio

    captured: dict = {}

    class _FakeResponse:
        status = 200

        async def json(self):
            return {"message": {"content": "ok", "tool_calls": []}, "done_reason": "stop"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _FakeSession:
        def __init__(self, *_a, **_k): ...
        def post(self, _url, json=None):
            captured["payload"] = json
            return _FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)
    adapter = OllamaAdapter(timeout_seconds=5)
    asyncio.run(
        adapter.complete(
            [{"role": "user", "content": "hi"}],
            model="ollama/qwen2.5-coder:7b-instruct",
        )
    )
    assert captured["payload"]["model"] == "qwen2.5-coder:7b-instruct"


def _patch_ollama_capture(monkeypatch) -> dict:
    """Install a fake aiohttp.ClientSession that records the posted payload."""
    captured: dict = {}

    class _FakeResponse:
        status = 200

        async def json(self):
            return {"message": {"content": "ok", "tool_calls": []}, "done_reason": "stop"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _FakeSession:
        def __init__(self, *_a, **_k): ...
        def post(self, _url, json=None):
            captured["payload"] = json
            return _FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)
    return captured


def test_ollama_text_fallback_fires_callback(monkeypatch) -> None:
    """When a model emits tool calls as JSON text (no structured tool_calls),
    the adapter parses them AND fires on_text_fallback with the 0-based turn."""
    import asyncio

    class _FakeResponse:
        status = 200

        async def json(self):
            # Tool call emitted as JSON text in content, structured list empty.
            return {
                "message": {
                    "content": '{"name": "write_file", "arguments": {"path": "x.py"}}',
                    "tool_calls": [],
                },
                "done_reason": "stop",
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _FakeSession:
        def __init__(self, *_a, **_k): ...
        def post(self, _url, json=None):
            return _FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)
    fires: list[int] = []
    adapter = OllamaAdapter(timeout_seconds=5, on_text_fallback=fires.append)
    tools = [{"name": "write_file", "description": "", "input_schema": {}}]
    resp = asyncio.run(
        adapter.complete(
            [{"role": "user", "content": "go"}],
            tools=tools,
            model="ollama/qwen2.5-coder:7b-instruct",
        )
    )
    # The text was parsed into a structured tool call...
    assert [c.name for c in resp.tool_calls] == ["write_file"]
    assert resp.content == ""
    # ...and the fallback hook fired once for turn 0.
    assert fires == [0]


def test_ollama_no_fallback_when_structured(monkeypatch) -> None:
    """A native structured tool_call must NOT trip the text-fallback hook."""
    import asyncio

    class _FakeResponse:
        status = 200

        async def json(self):
            return {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "write_file", "arguments": {"path": "x"}}}
                    ],
                },
                "done_reason": "stop",
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _FakeSession:
        def __init__(self, *_a, **_k): ...
        def post(self, _url, json=None):
            return _FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)
    fires: list[int] = []
    adapter = OllamaAdapter(timeout_seconds=5, on_text_fallback=fires.append)
    tools = [{"name": "write_file", "description": "", "input_schema": {}}]
    asyncio.run(
        adapter.complete(
            [{"role": "user", "content": "go"}], tools=tools, model="ollama/x"
        )
    )
    assert fires == []


def test_ollama_adapter_uses_configured_keep_alive(monkeypatch) -> None:
    """keep_alive on the adapter governs the /api/chat payload field."""
    import asyncio

    captured = _patch_ollama_capture(monkeypatch)
    adapter = OllamaAdapter(timeout_seconds=5, keep_alive="2h")
    asyncio.run(
        adapter.complete(
            [{"role": "user", "content": "hi"}],
            model="ollama/qwen2.5-coder:7b-instruct",
        )
    )
    assert captured["payload"]["keep_alive"] == "2h"


def test_ollama_payload_includes_temperature_and_seed(monkeypatch) -> None:
    """Configured temperature + seed must reach the /api/chat options dict."""
    import asyncio

    captured = _patch_ollama_capture(monkeypatch)
    adapter = OllamaAdapter(timeout_seconds=5, temperature=0.0, seed=42)
    asyncio.run(
        adapter.complete(
            [{"role": "user", "content": "hi"}],
            model="ollama/qwen2.5-coder:7b-instruct",
        )
    )
    opts = captured["payload"]["options"]
    assert opts["temperature"] == 0.0
    assert opts["seed"] == 42


def test_ollama_payload_omits_sampling_when_unset(monkeypatch) -> None:
    """Legacy back-compat: no temperature/seed configured ⇒ not sent (Ollama default)."""
    import asyncio

    captured = _patch_ollama_capture(monkeypatch)
    adapter = OllamaAdapter(timeout_seconds=5)  # defaults None
    asyncio.run(
        adapter.complete(
            [{"role": "user", "content": "hi"}],
            model="ollama/qwen2.5-coder:7b-instruct",
        )
    )
    opts = captured["payload"]["options"]
    assert "temperature" not in opts
    assert "seed" not in opts


def test_ollama_adapter_default_max_tokens_governs_num_predict(monkeypatch) -> None:
    """When the caller passes no max_tokens, adapter's default_max_tokens flows."""
    import asyncio

    captured = _patch_ollama_capture(monkeypatch)
    adapter = OllamaAdapter(timeout_seconds=5, default_max_tokens=2048)
    asyncio.run(
        adapter.complete(
            [{"role": "user", "content": "hi"}],
            model="ollama/qwen2.5-coder:7b-instruct",
        )
    )
    assert captured["payload"]["options"]["num_predict"] == 2048


def test_ollama_adapter_explicit_max_tokens_overrides_default(monkeypatch) -> None:
    """Callers (extract_learnings, distiller) keep their explicit budget."""
    import asyncio

    captured = _patch_ollama_capture(monkeypatch)
    adapter = OllamaAdapter(timeout_seconds=5, default_max_tokens=2048)
    asyncio.run(
        adapter.complete(
            [{"role": "user", "content": "hi"}],
            model="ollama/qwen2.5-coder:7b-instruct",
            max_tokens=512,
        )
    )
    assert captured["payload"]["options"]["num_predict"] == 512


def test_create_adapter_forwards_keep_alive_and_default_max_tokens() -> None:
    adapter = create_llm_adapter(
        "ollama/llama3.1", keep_alive="1h", default_max_tokens=8192
    )
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.keep_alive == "1h"
    assert adapter.default_max_tokens == 8192


def test_strip_thinking_blocks_drops_think_pair() -> None:
    """qwen3 / deepseek-r1 style: ``<think>…</think>`` followed by the answer."""
    from agora.fleet.llm_adapter import _strip_thinking_blocks

    text = "<think>let me plan</think>The answer is 42."
    assert _strip_thinking_blocks(text) == "The answer is 42."


def test_strip_thinking_blocks_drops_thinking_pair() -> None:
    """Gemma / Claude convention: ``<thinking>…</thinking>`` (longer tag)."""
    from agora.fleet.llm_adapter import _strip_thinking_blocks

    text = "<thinking>step 1, step 2</thinking>\nDone."
    assert _strip_thinking_blocks(text) == "Done."


def test_strip_thinking_blocks_handles_multiple_traces() -> None:
    from agora.fleet.llm_adapter import _strip_thinking_blocks

    # One space sits between "first" and "<think>"; no space after "</think>".
    # After removal the surrounding whitespace is preserved verbatim — the
    # function only strips outer whitespace, not internal collapsing.
    text = "<think>plan</think>first <think>more</think>second"
    assert _strip_thinking_blocks(text) == "first second"


def test_strip_thinking_blocks_no_trace_is_noop() -> None:
    """Models without reasoning traces (qwen2.5) must round-trip unchanged."""
    from agora.fleet.llm_adapter import _strip_thinking_blocks

    text = "plain content with no tags"
    assert _strip_thinking_blocks(text) == text


def test_strip_thinking_blocks_unterminated_drops_remainder() -> None:
    """Truncated reasoning trace → drop everything from the open tag.

    Emitting half a trace would mislead the tool-call parser into
    treating reasoning-prose as content.
    """
    from agora.fleet.llm_adapter import _strip_thinking_blocks

    text = "real answer <think>I was cut off mid-thought"
    assert _strip_thinking_blocks(text) == "real answer"


def test_strip_thinking_blocks_case_insensitive() -> None:
    from agora.fleet.llm_adapter import _strip_thinking_blocks

    text = "<THINK>caps</THINK>answer"
    assert _strip_thinking_blocks(text) == "answer"


def test_ollama_adapter_strips_thinking_in_response(monkeypatch) -> None:
    """The adapter must drop thinking blocks before returning LLMResponse."""
    import asyncio

    class _FakeResponse:
        status = 200

        async def json(self):
            return {
                "message": {
                    "content": "<think>reasoning</think>actual reply",
                    "tool_calls": [],
                },
                "done_reason": "stop",
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class _FakeSession:
        def __init__(self, *_a, **_k): ...
        def post(self, _url, json=None):  # noqa: ARG002
            return _FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)
    adapter = OllamaAdapter(timeout_seconds=5)
    resp = asyncio.run(
        adapter.complete(
            [{"role": "user", "content": "hi"}],
            model="ollama/qwen3:7b",
        )
    )
    assert resp.content == "actual reply"


def test_ollama_adapter_shapes_tool_results_bare_per_result() -> None:
    """Probe v7 (form B): one BARE tool-role message per result — content is the
    result VERBATIM, and NO protocol fields (tool_name/tool_call_id), no marker.
    The fields made gemma's renderer escape newlines; a bare message renders
    plainly (rendering arbitration)."""
    adapter = OllamaAdapter()
    msgs = adapter.format_tool_results(
        calls=[
            ToolCall(id="0", name="read_file", arguments={}),
            ToolCall(id="1", name="write_file", arguments={}),
        ],
        results=["apple\napricot\n", "wrote 3 bytes"],
    )
    assert isinstance(msgs, list) and len(msgs) == 2
    # Bare: role + verbatim content ONLY — no protocol fields, no marker.
    assert msgs[0] == {"role": "tool", "content": "apple\napricot\n"}
    assert msgs[1] == {"role": "tool", "content": "wrote 3 bytes"}
    assert all("[read_file#" not in m["content"] for m in msgs)


def test_anthropic_adapter_shapes_tool_results_as_blocks() -> None:
    from agora.fleet.llm_adapter import _AnthropicShape

    shape = _AnthropicShape()
    turn = shape.format_tool_results(
        calls=[ToolCall(id="call-1", name="write_file", arguments={})],
        results=["wrote"],
    )
    assert turn["role"] == "user"
    blocks = turn["content"]
    assert blocks[0]["type"] == "tool_result"
    assert blocks[0]["tool_use_id"] == "call-1"


def test_llm_response_dataclass_defaults() -> None:
    resp = LLMResponse(content="hi")
    assert resp.tool_calls == ()
    assert resp.usage == {}
    assert resp.stop_reason == ""


def test_toolcall_is_hashable_dataclass() -> None:
    call = ToolCall(id="1", name="x", arguments={"k": "v"})
    assert call.name == "x"
    assert call.arguments == {"k": "v"}


def test_llm_protocol_runtime_check(fake_matrix_client) -> None:  # noqa: ARG001
    from tests.conftest import FakeLLM

    llm = FakeLLM([])
    assert isinstance(llm, LLMProtocol)


# ---- Ollama serialisation (fix for interleaved turns on single-slot daemons) ----


class _SlowSession:
    """Context-manager aiohttp.ClientSession stub that records entry/exit times.

    The post() coroutine sleeps for ``response_delay`` seconds before returning
    a fake 200 JSON body. Tests use this to observe whether two concurrent
    OllamaAdapter.complete() calls overlap or serialise.
    """

    events: list[tuple[str, float]] = []

    def __init__(self, *args, **kwargs):  # noqa: ARG002
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def post(self, _url, json=None):  # noqa: ARG002
        return _SlowResponse(self)


class _SlowResponse:
    response_delay: float = 0.15
    _counter: int = 0

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        import asyncio as _a
        import time as _t

        _SlowResponse._counter += 1
        my_id = _SlowResponse._counter
        _SlowSession.events.append((f"enter-{my_id}", _t.monotonic()))
        await _a.sleep(self.response_delay)
        _SlowSession.events.append((f"reply-{my_id}", _t.monotonic()))
        self._my_id = my_id
        return self

    async def __aexit__(self, *a):
        import time as _t

        _SlowSession.events.append((f"exit-{self._my_id}", _t.monotonic()))
        return None

    status = 200

    async def json(self):
        return {
            "message": {"content": "ok", "tool_calls": []},
            "done_reason": "stop",
        }

    async def text(self):
        return ""


@pytest.fixture
def _reset_ollama_semaphores():
    """Clear the module-level semaphore cache so each test starts fresh."""
    from agora.fleet import llm_adapter as _mod

    before = dict(_mod._OLLAMA_SEMAPHORES)
    _mod._OLLAMA_SEMAPHORES.clear()
    _SlowSession.events.clear()
    _SlowResponse._counter = 0
    yield
    _mod._OLLAMA_SEMAPHORES.clear()
    _mod._OLLAMA_SEMAPHORES.update(before)


async def test_ollama_adapter_serialises_concurrent_calls(
    monkeypatch, _reset_ollama_semaphores
) -> None:
    """Two concurrent complete() calls with max_concurrent=1 must NOT overlap."""
    import asyncio

    monkeypatch.setattr("aiohttp.ClientSession", _SlowSession)

    adapter = OllamaAdapter(
        base_url="http://localhost:11434",
        timeout_seconds=5,
        num_ctx=None,
        max_concurrent=1,
    )

    async def _call():
        return await adapter.complete(
            [{"role": "user", "content": "hi"}], model="ollama/test"
        )

    await asyncio.gather(_call(), _call())

    # Parse the event log: (label, monotonic_time).
    by_label = {label: ts for label, ts in _SlowSession.events}
    # The second call's "enter" happens AFTER the first call's "exit"
    # (serial execution). Using strict '>=' is fine because asyncio's
    # monotonic clock is granular.
    assert by_label["exit-1"] <= by_label["enter-2"] + 0.01, (
        f"calls overlapped; events: {_SlowSession.events}"
    )


async def test_ollama_adapter_respects_custom_max_concurrent(
    monkeypatch, _reset_ollama_semaphores
) -> None:
    """max_concurrent=2 allows two overlapping calls."""
    import asyncio

    monkeypatch.setattr("aiohttp.ClientSession", _SlowSession)

    adapter = OllamaAdapter(
        base_url="http://localhost:11434",
        timeout_seconds=5,
        num_ctx=None,
        max_concurrent=2,
    )

    async def _call():
        return await adapter.complete(
            [{"role": "user", "content": "hi"}], model="ollama/test"
        )

    await asyncio.gather(_call(), _call())

    by_label = {label: ts for label, ts in _SlowSession.events}
    # Both "enter" events fire before either "exit" — overlapped.
    assert by_label["enter-2"] < by_label["exit-1"], (
        f"calls did not overlap; events: {_SlowSession.events}"
    )


async def test_ollama_semaphore_is_shared_across_adapter_instances(
    monkeypatch, _reset_ollama_semaphores
) -> None:
    """Two different OllamaAdapter instances pointing at the same base_url share a lock."""
    import asyncio

    monkeypatch.setattr("aiohttp.ClientSession", _SlowSession)

    a1 = OllamaAdapter(
        base_url="http://localhost:11434", timeout_seconds=5, num_ctx=None, max_concurrent=1
    )
    a2 = OllamaAdapter(
        base_url="http://localhost:11434", timeout_seconds=5, num_ctx=None, max_concurrent=1
    )

    async def _call(ad):
        return await ad.complete([{"role": "user", "content": "hi"}], model="ollama/test")

    await asyncio.gather(_call(a1), _call(a2))

    by_label = {label: ts for label, ts in _SlowSession.events}
    assert by_label["exit-1"] <= by_label["enter-2"] + 0.01, (
        f"instances did not share lock; events: {_SlowSession.events}"
    )
