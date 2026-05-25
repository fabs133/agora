"""Model-agnostic LLM adapter.

Defines :class:`LLMProtocol` — a small async interface covering tool-calling chat
completion. Production adapters for Anthropic (Claude), Ollama, and a subprocess
wrapper around the ``claude`` CLI are provided; tests inject a fake.

The protocol intentionally does not handle tool *execution* — that lives in
:mod:`agora.fleet.agent_runtime`. Each adapter only ferries messages and tool-call
intents between the model and the runtime, and knows how to shape its own
provider-specific assistant-turn and tool-result messages.

Non-blocking invariant: all IO is ``async``/``await`` with explicit timeouts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import aiohttp

from agora.core.errors import AgoraError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation in an assistant turn.

    ``id`` is the provider-assigned identifier the runtime echoes back in
    the tool-result message. ``name`` matches a registered inner tool;
    ``arguments`` is the parsed JSON object the model emitted.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    """One assistant turn from the underlying model.

    ``content`` is the plaintext component (often empty when the turn is
    pure tool-use). ``tool_calls`` are the tool invocations the runtime
    will execute and feed results back for. ``usage`` carries provider
    token counters; the LiteLLM adapter also surfaces ``cost_usd`` here.
    ``stop_reason`` is the provider's termination signal, propagated so
    callers can distinguish budget exhaustion from natural completion.
    """

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = ""


@runtime_checkable
class LLMProtocol(Protocol):
    """Minimal async interface every backend must satisfy.

    Adapters know how to ferry messages and tool-call intents between the
    model and the runtime, and how to shape their own provider-specific
    assistant-turn and tool-result message bodies. Tool *execution* is
    not part of this protocol — that lives in
    :mod:`agora.fleet.agent_runtime`. Production implementations:
    :class:`AnthropicAdapter`, :class:`OllamaAdapter`,
    :class:`LiteLLMAdapter`, and the subprocess-driven
    :class:`agora.fleet.claude_code_adapter.ClaudeCodeSubprocessAdapter`.
    Tests inject a fake.
    """

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        max_tokens: int = 4096,
    ) -> LLMResponse: ...

    def format_assistant_turn(self, response: LLMResponse) -> dict[str, Any]: ...

    def format_tool_results(
        self,
        calls: list[ToolCall],
        results: list[str],
    ) -> dict[str, Any]: ...


# ------------------------------------------------------------- Anthropic shape


class _AnthropicShape:
    """Default (Anthropic-style) assistant-turn + tool-result shaping.

    Shared by :class:`AnthropicAdapter` and anything else that speaks native
    ``tool_use`` / ``tool_result`` block arrays.
    """

    def format_assistant_turn(self, response: LLMResponse) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        if response.content:
            blocks.append({"type": "text", "text": response.content})
        for call in response.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
            )
        return {"role": "assistant", "content": blocks}

    def format_tool_results(
        self,
        calls: list[ToolCall],
        results: list[str],
    ) -> dict[str, Any]:
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": result,
            }
            for call, result in zip(calls, results, strict=True)
        ]
        return {"role": "user", "content": blocks}


# --------------------------------------------------------------- AnthropicAdapter


class AnthropicAdapter(_AnthropicShape):
    """Anthropic API implementation. Requires the ``anthropic`` extra installed."""

    def __init__(self, api_key: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover - import guard
            raise AgoraError(
                "anthropic package not installed. Install with `pip install agora[llm]`."
            ) from exc
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_seconds)
        self._timeout = timeout_seconds

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        resp = await self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        arguments=dict(getattr(block, "input", {}) or {}),
                    )
                )
        usage_obj = getattr(resp, "usage", None)
        usage = (
            {
                "input_tokens": getattr(usage_obj, "input_tokens", 0),
                "output_tokens": getattr(usage_obj, "output_tokens", 0),
            }
            if usage_obj
            else {}
        )
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tuple(tool_calls),
            usage=usage,
            stop_reason=getattr(resp, "stop_reason", "") or "",
        )


# ----------------------------------------------------------------- OllamaAdapter


# Module-level semaphores keyed by base_url so every OllamaAdapter that points
# at the same daemon shares one inflight-request slot. The orchestrator creates
# a fresh OllamaAdapter per task via its llm_factory, so an instance-level
# semaphore would not serialise across tasks; keying by base_url fixes that.
# Ollama's inference queue is single-slot by default, so concurrent requests
# interleave turns and confuse the model's conversation state.
_OLLAMA_SEMAPHORES: dict[tuple[str, int], asyncio.Semaphore] = {}


def _get_ollama_semaphore(base_url: str, max_concurrent: int) -> asyncio.Semaphore:
    """Return the shared semaphore for a (base_url, max_concurrent) pair."""
    key = (base_url, max(1, int(max_concurrent)))
    sem = _OLLAMA_SEMAPHORES.get(key)
    if sem is None:
        sem = asyncio.Semaphore(key[1])
        _OLLAMA_SEMAPHORES[key] = sem
    return sem


class OllamaAdapter:
    """Ollama HTTP adapter. Talks to a local or remote Ollama daemon.

    Uses Ollama's OpenAI-style ``role: tool`` messages for tool results instead of
    Anthropic-style ``tool_result`` blocks — many local models refuse to parse the
    block array shape.

    Concurrent ``complete()`` calls against the same ``base_url`` are serialised
    (see :func:`_get_ollama_semaphore`) because Ollama's inference queue has
    effectively one slot and parallel requests interleave and degrade quality.
    Set ``max_concurrent`` > 1 if your daemon is tuned for parallel inference
    (e.g. ``OLLAMA_NUM_PARALLEL=2`` with matching VRAM).
    """

    OLLAMA_PREFIX = "ollama/"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        num_ctx: int | None = 16384,
        max_concurrent: int = 1,
        default_model: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_concurrent = max(1, int(max_concurrent))
        self.timeout_seconds = timeout_seconds
        # Ollama's default num_ctx is 2048-4096 — too small once we include tool
        # schemas + fetched docs + turn history. Bumped so ~16 KB of fetched text
        # plus a 10-turn tool loop fits. None = use Ollama's default.
        self.num_ctx = num_ctx
        # Set when the adapter was constructed for a specific model — used
        # as the fallback when ``complete()`` is called with an empty model
        # (e.g. v2.3 plan-builder emits agents with ``model=""`` because the
        # model can't reliably pick a valid id; the harness owns the runtime
        # model choice and wires it through the factory).
        self.default_model = default_model.removeprefix(self.OLLAMA_PREFIX) or default_model

    # --- shaping ---

    def format_assistant_turn(self, response: LLMResponse) -> dict[str, Any]:
        tool_calls = [
            {
                "function": {
                    "name": call.name,
                    "arguments": call.arguments,
                }
            }
            for call in response.tool_calls
        ]
        turn: dict[str, Any] = {"role": "assistant", "content": response.content or ""}
        if tool_calls:
            turn["tool_calls"] = tool_calls
        return turn

    def format_tool_results(
        self,
        calls: list[ToolCall],
        results: list[str],
    ) -> dict[str, Any]:
        # Ollama accepts one tool message per call; agent_runtime will call us
        # once per turn with all results, so we fold them into a single message
        # by joining — callers that need one-per-tool invoke us per result.
        payload = "\n".join(
            f"[{call.name}#{call.id}] {res}" for call, res in zip(calls, results, strict=True)
        )
        tool_name = calls[0].name if calls else ""
        return {"role": "tool", "content": payload, "tool_name": tool_name}

    # --- completion ---

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str = "qwen2.5-coder:7b-instruct",
        max_tokens: int = 4096,
    ) -> LLMResponse:
        effective_model = (
            model.removeprefix(self.OLLAMA_PREFIX) or model or self.default_model
        )
        options: dict[str, Any] = {"num_predict": max_tokens}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        payload: dict[str, Any] = {
            "model": effective_model,
            "messages": (
                [{"role": "system", "content": system}] if system else []
            ) + messages,
            "stream": False,
            "keep_alive": "30m",
            "options": options,
        }
        if tools:
            payload["tools"] = [self._tool_to_ollama_shape(t) for t in tools]

        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds,
            sock_connect=10,
            sock_read=self.timeout_seconds,
        )
        semaphore = _get_ollama_semaphore(self.base_url, self.max_concurrent)
        try:
            async with semaphore, aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/api/chat", json=payload
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise AgoraError(
                            f"Ollama HTTP {resp.status}: {body[:200]}"
                        )
                    data = await resp.json()
        except TimeoutError as exc:
            raise AgoraError(
                f"Ollama request timed out after {self.timeout_seconds}s; is the daemon responsive?"
            ) from exc
        except aiohttp.ClientConnectorError as exc:
            raise AgoraError(
                f"Cannot reach Ollama at {self.base_url} — is `ollama serve` running?"
            ) from exc

        msg = data.get("message", {}) or {}
        content = msg.get("content", "") or ""
        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls", []) or []):
            fn = tc.get("function", {}) or {}
            args = fn.get("arguments", {}) or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            tool_calls.append(
                ToolCall(id=str(i), name=fn.get("name", ""), arguments=dict(args))
            )
        # Fallback: some models (e.g. qwen2.5-coder:7b on Ollama) emit tool
        # calls as JSON text in `content` instead of the structured
        # `tool_calls` field. Extract them so the runtime can execute them.
        if not tool_calls and tools and content.strip():
            parsed = _parse_tool_calls_from_text(content, tools)
            if parsed:
                tool_calls = parsed
                content = ""  # consumed by the tool call path
        usage = {
            "input_tokens": int(data.get("prompt_eval_count", 0) or 0),
            "output_tokens": int(data.get("eval_count", 0) or 0),
        }
        return LLMResponse(
            content=content,
            tool_calls=tuple(tool_calls),
            usage=usage,
            stop_reason=data.get("done_reason", "") or "",
        )

    @staticmethod
    def _tool_to_ollama_shape(tool: dict[str, Any]) -> dict[str, Any]:  # noqa: D401
        """Translate our Anthropic-style tool schema to Ollama's OpenAI-style."""
        return {
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or tool.get("parameters") or {},
            },
        }


def _parse_tool_calls_from_text(
    text: str, tools: list[dict[str, Any]]
) -> list[ToolCall]:
    """Extract tool calls the model emitted as JSON text instead of via ``tool_calls``.

    Accepts either a single JSON object or a JSON array of objects, each shaped
    ``{"name": "...", "arguments": {...}}``. Matches names against the provided
    ``tools`` list; unknown names are ignored. Strips ```...``` code fences first.
    """
    import re

    valid_names = {t.get("name") for t in tools if isinstance(t, dict)}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    candidates: list[Any]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find the first balanced JSON object/array in the text.
        match = re.search(r"(\[.*\]|\{.*\})", cleaned, re.DOTALL)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, list):
        candidates = parsed
    elif isinstance(parsed, dict):
        candidates = [parsed]
    else:
        return []

    result: list[ToolCall] = []
    for i, obj in enumerate(candidates):
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or obj.get("tool") or obj.get("function")
        if not isinstance(name, str) or name not in valid_names:
            continue
        args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {"_raw": str(args)}
        result.append(ToolCall(id=str(i), name=name, arguments=args))
    return result


# -------------------------------------------------------------- LiteLLMAdapter


#: Provider prefixes that LiteLLM recognises natively. When ``create_llm_adapter``
#: sees one of these prefixes (e.g. ``"openai/gpt-4o-mini"``), it routes to
#: :class:`LiteLLMAdapter`. Extend this if a provider you use is missing —
#: LiteLLM supports ~100 but we only whitelist the common ones to avoid
#: accidentally intercepting model strings meant for Agora's native adapters.
#:
#: Note: ``"anthropic/"`` routes to LiteLLM; the direct ``AnthropicAdapter``
#: path uses the bare ``"claude-*"`` string (e.g. ``"claude-sonnet-4-20250514"``).
#: Use the LiteLLM path by default; the direct path is kept for back-compat with
#: existing runner scripts that use the bare form.
LITELLM_PROVIDER_PREFIXES: tuple[str, ...] = (
    "openai/",
    "anthropic/",
    "gemini/",
    "vertex_ai/",
    "bedrock/",
    "azure/",
    "mistral/",
    "groq/",
    "together_ai/",
    "xai/",
    "cohere/",
    "deepseek/",
    "perplexity/",
    "fireworks_ai/",
    "openrouter/",
)


def _is_litellm_model(model: str) -> bool:
    """Return True if ``model`` should route through LiteLLM.

    Accepts either a provider-prefixed string (``"openai/gpt-4o-mini"``) or
    an explicit ``"litellm/"`` override (``"litellm/<anything>"``) for
    providers not in :data:`LITELLM_PROVIDER_PREFIXES`.
    """
    if model.startswith("litellm/"):
        return True
    return any(model.startswith(p) for p in LITELLM_PROVIDER_PREFIXES)


class LiteLLMAdapter:
    """Multi-provider adapter backed by ``litellm``.

    Routes to OpenAI, Anthropic, Gemini, Mistral, Bedrock, and many more
    via a single normalised interface. Tool-call shape is OpenAI-strict
    (one ``role: "tool"`` message per ``tool_call_id``), which is what
    real OpenAI validates — unlike :class:`OllamaAdapter` which can fold
    multiple results into one message because Ollama is lenient.

    API keys are picked up from the environment by LiteLLM natively
    (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, ``GEMINI_API_KEY``, …).
    You don't pass them here.

    Model string routing:
      - ``"openai/gpt-4o-mini"`` → OpenAI
      - ``"anthropic/claude-haiku-4-5"`` → Anthropic
      - ``"gemini/gemini-1.5-flash"`` → Google
      - ``"litellm/<provider>/<model>"`` → explicit override
    """

    LITELLM_PREFIX = "litellm/"

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        default_model: str = "",
    ) -> None:
        try:
            import litellm  # type: ignore  # noqa: F401
        except ImportError as exc:  # pragma: no cover - import guard
            raise AgoraError(
                "litellm package not installed. "
                "Install with `pip install agora[litellm]` "
                "(or `pip install litellm>=1.52`)."
            ) from exc
        self._timeout = float(timeout_seconds)
        # Strip our explicit override prefix; provider prefixes like
        # ``openai/`` are kept verbatim because LiteLLM wants them.
        self.default_model = default_model.removeprefix(self.LITELLM_PREFIX)

    # --- shaping (OpenAI / LiteLLM native) ---

    def format_assistant_turn(self, response: LLMResponse) -> dict[str, Any]:
        """OpenAI assistant turn: string content + parallel ``tool_calls`` list.

        ``tool_calls[i].function.arguments`` is a JSON-serialised string
        (not an object) per OpenAI's spec. LiteLLM propagates this shape.
        """
        tool_calls = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in response.tool_calls
        ]
        # OpenAI allows `content: null` when tool_calls is present; empty
        # string is also accepted and safer for providers that differ.
        turn: dict[str, Any] = {
            "role": "assistant",
            "content": response.content or "",
        }
        if tool_calls:
            turn["tool_calls"] = tool_calls
        return turn

    def format_tool_results(
        self,
        calls: list[ToolCall],
        results: list[str],
    ) -> list[dict[str, Any]]:
        """One ``role: "tool"`` message per tool_call_id.

        Returning a list (not a dict) so :func:`_append_turn` extends
        ``messages`` properly. OpenAI validates that every assistant
        ``tool_calls[].id`` has a matching ``role: tool`` reply with the
        same ``tool_call_id`` — folding would break that invariant for
        strict providers.
        """
        return [
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            }
            for call, result in zip(calls, results, strict=True)
        ]

    # --- completion ---

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        max_tokens: int = 4096,
    ) -> LLMResponse:
        import litellm  # type: ignore  # local import keeps top-level lazy

        effective_model = (model or self.default_model).removeprefix(
            self.LITELLM_PREFIX
        )
        if not effective_model:
            raise AgoraError("LiteLLMAdapter.complete: no model specified")

        # OpenAI-shape messages: system goes as a separate role-system entry
        # at the head, not as a sibling ``system`` kwarg.
        composed_messages: list[dict[str, Any]] = []
        if system:
            composed_messages.append({"role": "system", "content": system})
        composed_messages.extend(messages)

        kwargs: dict[str, Any] = {
            "model": effective_model,
            "messages": composed_messages,
            "max_tokens": max_tokens,
            "timeout": self._timeout,
        }
        if tools:
            kwargs["tools"] = [self._tool_to_openai_shape(t) for t in tools]
            # Let the model decide whether to call tools; ``"auto"`` matches
            # the default for most providers but we pin it for consistency.
            kwargs["tool_choice"] = "auto"

        try:
            resp = await litellm.acompletion(**kwargs)
        except TimeoutError as exc:
            raise AgoraError(
                f"LiteLLM request timed out after {self._timeout}s for model "
                f"{effective_model!r}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — surface any provider error
            raise AgoraError(
                f"LiteLLM call to {effective_model!r} failed: {exc}"
            ) from exc

        return self._parse_response(resp)

    @staticmethod
    def _parse_response(resp: Any) -> LLMResponse:
        """Extract content + tool calls + usage from a LiteLLM response.

        LiteLLM normalises every provider's shape to OpenAI's ChatCompletion
        object, so we treat the response as such. ``tool_calls[i].function.
        arguments`` arrives as a JSON string; we parse it into a dict.

        Also computes per-call USD cost via :func:`litellm.completion_cost`
        and stores it under ``usage['cost_usd']`` — lets the harness report
        total cost per run for cross-provider comparison. Failures in
        cost-lookup are non-fatal (some proxies strip model metadata); the
        response still returns, just without the cost field.
        """
        try:
            choice = resp.choices[0]
            message = choice.message
        except (AttributeError, IndexError) as exc:
            raise AgoraError(f"LiteLLM returned unparseable response: {exc}") from exc

        content = getattr(message, "content", "") or ""
        tool_calls_raw = getattr(message, "tool_calls", None) or []
        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(tool_calls_raw):
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            call_id = getattr(tc, "id", "") or f"call_{i}"
            name = getattr(fn, "name", "") or ""
            raw_args = getattr(fn, "arguments", "") or ""
            args: dict[str, Any]
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str):
                stripped = raw_args.strip()
                if not stripped:
                    args = {}
                else:
                    try:
                        parsed = json.loads(stripped)
                        args = parsed if isinstance(parsed, dict) else {"_raw": parsed}
                    except json.JSONDecodeError:
                        args = {"_raw": stripped}
            else:
                args = {"_raw": str(raw_args)}
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=args))

        usage_obj = getattr(resp, "usage", None)
        usage: dict[str, Any] = {}
        if usage_obj is not None:
            # LiteLLM mirrors OpenAI's usage shape. Some providers report
            # prompt_tokens / completion_tokens instead of input/output.
            in_tok = getattr(usage_obj, "prompt_tokens", None)
            out_tok = getattr(usage_obj, "completion_tokens", None)
            if in_tok is None:
                in_tok = getattr(usage_obj, "input_tokens", 0)
            if out_tok is None:
                out_tok = getattr(usage_obj, "output_tokens", 0)
            usage = {
                "input_tokens": int(in_tok or 0),
                "output_tokens": int(out_tok or 0),
            }

        # Per-call cost in USD. Non-fatal if the lookup fails; some
        # providers/proxies strip the model name from the response and
        # completion_cost falls back to 0 or raises.
        try:
            import litellm  # type: ignore

            cost = litellm.completion_cost(completion_response=resp)
            if cost and cost > 0:
                usage["cost_usd"] = float(cost)
        except Exception as exc:  # noqa: BLE001
            logger.debug("completion_cost lookup failed: %s", exc)

        return LLMResponse(
            content=content,
            tool_calls=tuple(tool_calls),
            usage=usage,
            stop_reason=getattr(choice, "finish_reason", "") or "",
        )

    @staticmethod
    def _tool_to_openai_shape(tool: dict[str, Any]) -> dict[str, Any]:
        """Translate Agora's Anthropic-style tool schema to OpenAI function shape.

        Agora stores tool definitions with ``input_schema`` (Anthropic's key);
        OpenAI/LiteLLM expects ``function.parameters``. Keep ``description``
        verbatim.
        """
        return {
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema")
                or tool.get("parameters")
                or {},
            },
        }


# ------------------------------------------------------------------- Factory


def create_llm_adapter(model: str, **kwargs: Any) -> LLMProtocol:
    """Route a model string to the right adapter.

    Model-string conventions:

    - ``ollama/<model>``        → :class:`OllamaAdapter` (no auth, local daemon)
    - ``claude-code/*``         → :class:`ClaudeCodeSubprocessAdapter` (Claude CLI subscription)
    - ``claude-*``              → :class:`AnthropicAdapter` (direct Anthropic API, requires ``api_key``)
    - ``<provider>/<model>``    → :class:`LiteLLMAdapter` for any provider prefix in
      :data:`LITELLM_PROVIDER_PREFIXES` (``openai/``, ``anthropic/``, ``gemini/``,
      ``mistral/``, ``groq/``, ``bedrock/``, ``azure/``, ``xai/``, …). API keys
      read from env by LiteLLM natively.
    - ``litellm/<anything>``    → :class:`LiteLLMAdapter` explicit override for
      providers not in the prefix whitelist.

    kwargs:
      ``api_key`` — for the direct Anthropic path.
      ``base_url``, ``num_ctx``, ``max_concurrent`` — for the Ollama path.
      ``timeout_seconds`` — per-adapter HTTP / subprocess timeout.
      ``binary``, ``allow`` — for the Claude Code subprocess path.
    """
    timeout = float(kwargs.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)

    if model.startswith("ollama/"):
        ollama_kwargs: dict[str, Any] = {
            "base_url": kwargs.get("base_url", "http://localhost:11434"),
            "timeout_seconds": timeout,
            "default_model": model,
        }
        # Forward only when the caller asked — preserves today's defaults
        # (num_ctx=16384, max_concurrent=1) when callers don't specify.
        if "num_ctx" in kwargs:
            ollama_kwargs["num_ctx"] = kwargs["num_ctx"]
        if "max_concurrent" in kwargs:
            ollama_kwargs["max_concurrent"] = kwargs["max_concurrent"]
        return OllamaAdapter(**ollama_kwargs)

    if model.startswith("claude-code/"):
        # Lazy import to avoid circular (claude_code_adapter imports from here).
        from agora.fleet.claude_code_adapter import ClaudeCodeSubprocessAdapter

        return ClaudeCodeSubprocessAdapter(
            binary=kwargs.get("binary", "claude"),
            allow=bool(kwargs.get("allow", False)),
            timeout_seconds=timeout,
        )

    # Provider-prefixed or litellm/ override: route through LiteLLM. Must
    # come BEFORE the ``claude-*`` branch so ``anthropic/claude-haiku-4-5``
    # gets LiteLLM (not mis-parsed as an Anthropic direct model id).
    if _is_litellm_model(model):
        return LiteLLMAdapter(timeout_seconds=timeout, default_model=model)

    if model.startswith("claude-"):
        api_key = kwargs.get("api_key")
        if not api_key:
            raise AgoraError(
                "Anthropic API-key path requires api_key. "
                "For subscription users, use model='claude-code/subscription' "
                "(opt-in via AGORA_ALLOW_CLAUDE_SUBPROCESS=1), or use the "
                "multi-provider path model='anthropic/claude-<model>' which "
                "picks up ANTHROPIC_API_KEY from the env."
            )
        return AnthropicAdapter(api_key=api_key, timeout_seconds=timeout)

    raise AgoraError(
        f"no adapter for model {model!r}. Use ollama/<name>, claude-code/*, "
        f"claude-* (direct), or a LiteLLM provider prefix "
        f"(openai/, anthropic/, gemini/, mistral/, …)."
    )
