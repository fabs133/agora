"""Model-agnostic LLM adapter.

Defines :class:`LLMProtocol` — a small async interface covering tool-calling chat
completion. The backend seam has one live implementation, :class:`OllamaAdapter`
(local Ollama daemon); tests inject a fake. Other backends are re-added through
the bench pipeline with evidence, not kept as dead code.

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
from collections.abc import Callable
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
    token counters (input/output). ``stop_reason`` is the termination signal,
    propagated so
    callers can distinguish budget exhaustion from natural completion.
    """

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = ""
    # S7 (run 2.4): the model's reasoning trace, surfaced instead of discarded —
    # either the provider's separate ``thinking`` field or the ``<think>…</think>``
    # blocks stripped from content. The runtime's reasoning-salvage nudge re-prompts
    # with this verbatim when a turn produces only reasoning and no tool call.
    thinking: str = ""


@runtime_checkable
class LLMProtocol(Protocol):
    """Minimal async interface every backend must satisfy.

    Adapters know how to ferry messages and tool-call intents between the
    model and the runtime, and how to shape their own provider-specific
    assistant-turn and tool-result message bodies. Tool *execution* is
    not part of this protocol — that lives in
    :mod:`agora.fleet.agent_runtime`. The one production implementation is
    :class:`OllamaAdapter`; tests inject a fake.
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
    a ``tool_result`` block-array shape — many local models refuse to parse the
    block-array form.

    Concurrent ``complete()`` calls against the same ``base_url`` are serialised
    (see :func:`_get_ollama_semaphore`) because Ollama's inference queue has
    effectively one slot and parallel requests interleave and degrade quality.
    Set ``max_concurrent`` > 1 if your daemon is tuned for parallel inference
    (e.g. ``OLLAMA_NUM_PARALLEL=2`` with matching VRAM).
    """

    OLLAMA_PREFIX = "ollama/"

    def __init__(
        self,
        base_url: str,  # required config-shaped endpoint — no localhost default; inject from Settings.ollama_base_url
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        num_ctx: int | None = 16384,
        max_concurrent: int = 1,
        default_model: str = "",
        keep_alive: str = "30m",
        default_max_tokens: int = 4096,
        temperature: float | None = None,
        seed: int | None = None,
        on_text_fallback: Callable[[int], None] | None = None,
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
        # How long Ollama keeps the model resident after the response.
        # Per /api/generate spec: accepts "30m", "1h", "0" (evict immediately).
        self.keep_alive = keep_alive
        # Fallback for ``complete()`` when callers don't pass max_tokens —
        # mirrors the ``default_model`` pattern. agent_runtime's main loop
        # passes ``model=`` but no max_tokens, so the profile's value flows
        # through here.
        self.default_max_tokens = int(default_max_tokens)
        # Sampling controls. None ⇒ omit from the options dict so Ollama uses
        # its own default (preserves behaviour for legacy callers that don't
        # pass these). Profile-driven runs pass concrete values so the value
        # recorded in run.jsonl is the one the daemon actually applied.
        self.temperature = temperature
        self.seed = seed
        # Optional observability hook. Invoked with the current tool-loop
        # iteration index each time ``_parse_tool_calls_from_text`` produces a
        # non-empty result (the model emitted tool calls as JSON text instead
        # of via the structured ``tool_calls`` field). Default None = no-op, so
        # non-observer paths are unaffected. The runtime sets this per task and
        # owns the authoritative iteration index it records.
        self.on_text_fallback = on_text_fallback
        # Per-instance tool-bearing-turn counter, surfaced to the callback so a
        # caller that doesn't track iterations still gets a 0-based index. The
        # adapter is constructed fresh per task, so this counts that task's
        # turns.
        self._tool_turn_index = -1

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
    ) -> list[dict[str, Any]]:
        # Probe v7 (form B): ONE bare tool-role message per result — content is
        # the result string VERBATIM, and NO protocol fields (tool_name /
        # tool_call_id). The rendering arbitration showed that including those
        # fields makes gemma's daemon renderer wrap the tool result as structure
        # and escape its newlines (\n → \\n), which the model then reproduces as
        # literal "\n"; a bare content message renders plainly with real newline
        # bytes. Correlation is by order + content, which is enough for the probe.
        # No marker, so a verbatim copy copies only the bytes given.
        return [{"role": "tool", "content": res} for res in results]

    # --- completion ---

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str = "qwen2.5-coder:7b-instruct",
        max_tokens: int | None = None,
    ) -> LLMResponse:
        effective_model = (
            model.removeprefix(self.OLLAMA_PREFIX) or model or self.default_model
        )
        # None signals "use the adapter-level default" — call sites that pass
        # a smaller budget explicitly (extract_learnings → 1024, distiller →
        # 1024, derive_test_intent → 512) keep their override.
        effective_max_tokens = (
            self.default_max_tokens if max_tokens is None else int(max_tokens)
        )
        options: dict[str, Any] = {"num_predict": effective_max_tokens}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        # Ollama /api/chat accepts ``temperature`` and ``seed`` as standard
        # sampling options. Only send them when configured so unconfigured
        # callers keep the daemon's defaults.
        if self.temperature is not None:
            options["temperature"] = float(self.temperature)
        if self.seed is not None:
            options["seed"] = int(self.seed)
        payload: dict[str, Any] = {
            "model": effective_model,
            "messages": (
                [{"role": "system", "content": system}] if system else []
            ) + messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": options,
        }
        if tools:
            payload["tools"] = [self._tool_to_ollama_shape(t) for t in tools]
            # Count tool-bearing turns so the text-fallback hook can report a
            # 0-based iteration index even for callers that don't track it.
            self._tool_turn_index += 1

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
        raw_content = msg.get("content", "") or ""
        # Reasoning-trace models (qwen3, gemma4, deepseek-r1, etc.) emit
        # ``<think>…</think>`` blocks that the framework's tool-call
        # parser and postcondition system don't model. Strip them before
        # any downstream consumer sees the text. Ollama also surfaces the
        # trace under a separate ``thinking`` key on the message. S7: capture
        # BOTH (separate key + inline blocks) into LLMResponse.thinking so the
        # runtime can salvage a reasoning-only turn instead of discarding the work.
        content = _strip_thinking_blocks(raw_content)
        thinking = "\n".join(
            t for t in (
                (msg.get("thinking", "") or "").strip(),
                _extract_thinking_blocks(raw_content),
            ) if t
        )
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
                if self.on_text_fallback is not None:
                    try:
                        self.on_text_fallback(self._tool_turn_index)
                    except Exception as exc:  # noqa: BLE001 — telemetry, never fail
                        logger.debug("on_text_fallback hook raised: %s", exc)
        usage = {
            "input_tokens": int(data.get("prompt_eval_count", 0) or 0),
            "output_tokens": int(data.get("eval_count", 0) or 0),
        }
        return LLMResponse(
            content=content,
            tool_calls=tuple(tool_calls),
            usage=usage,
            stop_reason=data.get("done_reason", "") or "",
            thinking=thinking,
        )

    @staticmethod
    def _tool_to_ollama_shape(tool: dict[str, Any]) -> dict[str, Any]:  # noqa: D401
        """Translate our block-style tool schema to Ollama's OpenAI-style."""
        return {
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or tool.get("parameters") or {},
            },
        }


#: Tag pairs used by reasoning-trace models to wrap inline reasoning.
#: ``<think>`` is qwen3 / deepseek-r1 style; ``<thinking>`` is the Gemma
#: convention. Both forms are stripped from Ollama output before tool-call
#: parsing because the framework's tool-call grammar (and postcondition
#: predicates) don't model reasoning traces.
_THINKING_TAG_PAIRS: tuple[tuple[str, str], ...] = (
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
)


def _strip_thinking_blocks(text: str) -> str:
    """Remove ``<think>…</think>`` / ``<thinking>…</thinking>`` blocks from text.

    Reasoning models (qwen3, gemma3/4, deepseek-r1) inline their chain of
    thought between tagged blocks before the actual answer. Agora's
    tool-call parser and runtime postconditions weren't built for
    reasoning traces, so we drop them here at the boundary. Both the
    `<think>` and `<thinking>` conventions are recognised; tags are
    matched case-insensitively. Unterminated opens (the model was
    truncated mid-trace) drop the rest of the string from the open tag
    onward — better to emit nothing than emit half a reasoning trace
    that downstream parsers will mistake for content.
    """
    if not text or "<" not in text:
        return text
    lower = text.lower()
    chunks: list[str] = []
    pos = 0
    while pos < len(text):
        # Find the nearest opening tag (if any) starting from pos.
        nearest_open = -1
        nearest_close = ""
        for open_tag, close_tag in _THINKING_TAG_PAIRS:
            idx = lower.find(open_tag, pos)
            if idx != -1 and (nearest_open == -1 or idx < nearest_open):
                nearest_open = idx
                nearest_close = close_tag
        if nearest_open == -1:
            chunks.append(text[pos:])
            break
        chunks.append(text[pos:nearest_open])
        close_idx = lower.find(nearest_close, nearest_open + len(nearest_close))
        if close_idx == -1:
            # Unterminated trace — drop from the open tag onward.
            break
        pos = close_idx + len(nearest_close)
    return "".join(chunks).strip()


def _extract_thinking_blocks(text: str) -> str:
    """Return the concatenated INNER text of all ``<think>``/``<thinking>`` blocks
    in ``text`` (the companion to :func:`_strip_thinking_blocks`, which removes
    them). Used by S7 to surface the model's discarded reasoning to the runtime.
    An unterminated open captures to end-of-string."""
    if not text or "<" not in text:
        return ""
    lower = text.lower()
    parts: list[str] = []
    pos = 0
    while pos < len(text):
        nearest_open = -1
        open_len = 0
        nearest_close = ""
        for open_tag, close_tag in _THINKING_TAG_PAIRS:
            idx = lower.find(open_tag, pos)
            if idx != -1 and (nearest_open == -1 or idx < nearest_open):
                nearest_open = idx
                open_len = len(open_tag)
                nearest_close = close_tag
        if nearest_open == -1:
            break
        inner_start = nearest_open + open_len
        close_idx = lower.find(nearest_close, inner_start)
        if close_idx == -1:
            parts.append(text[inner_start:])  # unterminated — take the rest
            break
        parts.append(text[inner_start:close_idx])
        pos = close_idx + len(nearest_close)
    return "\n".join(p.strip() for p in parts if p.strip())


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


# ------------------------------------------------------------------- Factory


def create_llm_adapter(model: str, **kwargs: Any) -> LLMProtocol:
    """Route a model string to an adapter. This is the backend SEAM: one
    interface (:class:`LLMProtocol`), one implementation (:class:`OllamaAdapter`).

    Only ``ollama/<model>`` is supported. Additional backends are re-added
    through the bench pipeline WITH EVIDENCE, not as kept dead code — so the
    factory stays a single, live branch rather than a menu of untested paths.

    kwargs (all optional, forwarded to :class:`OllamaAdapter` when present):
      ``base_url``, ``num_ctx``, ``max_concurrent``, ``keep_alive``,
      ``default_max_tokens``, ``temperature``, ``seed``, ``timeout_seconds``.
    """
    timeout = float(kwargs.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)

    if model.startswith("ollama/"):
        if "base_url" not in kwargs:
            # Config-shaped endpoint: no localhost default here (that default
            # lives once, in Settings.ollama_base_url). Composition roots inject it.
            raise AgoraError(
                "create_llm_adapter requires an explicit base_url for ollama/* "
                "models; inject Settings.ollama_base_url from the composition root."
            )
        ollama_kwargs: dict[str, Any] = {
            "base_url": kwargs["base_url"],
            "timeout_seconds": timeout,
            "default_model": model,
        }
        # Forward only when the caller asked — preserves today's defaults
        # (num_ctx=16384, max_concurrent=1, keep_alive="30m",
        # default_max_tokens=4096) when callers don't specify.
        for opt_key in (
            "num_ctx",
            "max_concurrent",
            "keep_alive",
            "default_max_tokens",
            "temperature",
            "seed",
        ):
            if opt_key in kwargs:
                ollama_kwargs[opt_key] = kwargs[opt_key]
        return OllamaAdapter(**ollama_kwargs)

    raise AgoraError(
        f"no adapter for model {model!r}. Only ollama/<name> is supported; "
        f"add other backends via the bench pipeline with evidence."
    )
