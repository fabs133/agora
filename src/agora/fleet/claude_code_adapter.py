"""Claude Code subprocess adapter.

Opt-in LLM backend that spawns the ``claude`` CLI as a subprocess for each turn.
Uses whatever auth Claude Code is logged in with — including claude.ai
subscriptions — at the cost of **no native tool_use**. Tool calls are simulated
by instructing the model to respond as strict JSON.

**Caveats** (surface to user at construction / invocation):

- Anthropic explicitly discourages third-party products from using claude.ai
  login. This adapter is a pragmatic workaround for individual subscription
  users; it is not an officially sanctioned integration.
- Prompted-JSON tool calling is less reliable than native ``tool_use`` blocks.
  Use this adapter for text-heavy roles (architect, reviewer) and prefer
  :class:`~agora.fleet.llm_adapter.OllamaAdapter` for tool-heavy roles.
- Every call spawns a subprocess — startup cost dominates small turns.

Non-blocking: uses ``asyncio.create_subprocess_exec`` + ``asyncio.wait_for``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from typing import Any

from agora.core.errors import AgoraError
from agora.fleet.llm_adapter import LLMResponse, ToolCall, _AnthropicShape

logger = logging.getLogger(__name__)

DEFAULT_SUBPROCESS_TIMEOUT = 300.0

_JSON_CONTRACT = (
    "Respond as a single JSON object with exactly two keys:\n"
    "  \"content\": string (your text reply, may be empty).\n"
    "  \"tool_calls\": array of objects, each with \"name\" (string) and\n"
    "                 \"arguments\" (object matching that tool's schema).\n"
    "If no tool call is needed, return \"tool_calls\": []. "
    "Do not wrap the JSON in code fences. Do not add any other text."
)


class ClaudeCodeSubprocessAdapter(_AnthropicShape):
    """Speaks to ``claude -p`` via subprocess. Inherits Anthropic-shape messages."""

    def __init__(
        self,
        binary: str = "claude",
        allow: bool = False,
        timeout_seconds: float = DEFAULT_SUBPROCESS_TIMEOUT,
    ) -> None:
        resolved = shutil.which(binary)
        if not resolved:
            raise AgoraError(
                f"cannot find '{binary}' on PATH. Install Claude Code and run `claude login`,"
                " or switch to model='ollama/...'."
            )
        self.binary = resolved
        self.allow = allow
        self.timeout_seconds = timeout_seconds

    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str = "claude-code/subscription",  # noqa: ARG002 — routing hint only
        max_tokens: int = 4096,  # noqa: ARG002 — CLI chooses its own budget
    ) -> LLMResponse:
        if not self.allow:
            raise AgoraError(
                "Claude Code subprocess adapter is disabled. Set "
                "AGORA_ALLOW_CLAUDE_SUBPROCESS=1 to enable; read the README "
                "section on ToS caveats first."
            )

        prompt = self._render_prompt(messages=messages, system=system, tools=tools or [])
        stdout = await self._invoke(prompt)
        return self._parse_response(stdout)

    # ------------------------------------------------------------------- helpers

    async def _invoke(self, prompt: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                "-p",
                prompt,
                "--output-format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AgoraError(f"claude binary vanished: {exc}") from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise AgoraError(
                f"claude subprocess exceeded {self.timeout_seconds}s timeout"
            ) from exc

        if proc.returncode != 0:
            err = stderr_b.decode(errors="replace").strip()
            raise AgoraError(
                f"claude subprocess exited {proc.returncode}: {err[:300] or '(no stderr)'}"
            )
        return stdout_b.decode(errors="replace")

    def _render_prompt(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
    ) -> str:
        parts: list[str] = []
        if system:
            parts.append(f"<system>\n{system}\n</system>")
        if tools:
            schemas = [
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema") or t.get("parameters") or {},
                }
                for t in tools
            ]
            parts.append("<tools>\n" + json.dumps(schemas, indent=2) + "\n</tools>")
        parts.append("<conversation>\n" + _flatten_messages(messages) + "\n</conversation>")
        parts.append("<response_contract>\n" + _JSON_CONTRACT + "\n</response_contract>")
        return "\n\n".join(parts)

    @staticmethod
    def _parse_response(stdout: str) -> LLMResponse:
        raw = stdout.strip()
        if not raw:
            return LLMResponse(content="", tool_calls=(), usage={}, stop_reason="empty")

        # `claude -p --output-format json` wraps the assistant text in a result object.
        # Try that first; fall back to parsing the raw text as our JSON contract.
        inner = _extract_cli_result(raw)
        body = _extract_json_object(inner if inner is not None else raw)
        if body is None:
            # Model refused the JSON contract — treat as plain content.
            return LLMResponse(content=(inner or raw), tool_calls=(), stop_reason="non_json")

        content = str(body.get("content", "")).strip()
        tool_calls_raw = body.get("tool_calls") or []
        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(tool_calls_raw):
            if not isinstance(tc, dict):
                continue
            name = tc.get("name")
            if not isinstance(name, str) or not name:
                continue
            args = tc.get("arguments", {}) or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"_raw": str(args)}
            tool_calls.append(ToolCall(id=str(i), name=name, arguments=args))
        return LLMResponse(
            content=content,
            tool_calls=tuple(tool_calls),
            usage={},
            stop_reason="end_turn",
        )


# ----------------------------------------------------------- helper functions


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Collapse an Anthropic-shape message list into a plain textual transcript.

    Tool use / tool result blocks are rendered inline as JSON so the model can
    reason about prior actions without a native block schema.
    """
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        lines.append(f"--- {role} ---")
        if isinstance(content, str):
            lines.append(content)
        elif isinstance(content, list):
            for block in content:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "text":
                    lines.append(block.get("text", ""))
                elif btype == "tool_use":
                    lines.append(
                        f"[tool_use {block.get('name')} id={block.get('id')}] "
                        + json.dumps(block.get("input", {}))
                    )
                elif btype == "tool_result":
                    lines.append(
                        f"[tool_result id={block.get('tool_use_id')}] "
                        + str(block.get("content", ""))
                    )
                else:
                    lines.append(json.dumps(block))
        else:
            lines.append(json.dumps(content))
    return "\n".join(lines)


_CLI_RESULT_KEYS = ("result", "response", "output", "text")


def _extract_cli_result(raw: str) -> str | None:
    """Pull the assistant text out of Claude Code's ``--output-format json`` wrapper."""
    try:
        wrapper = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(wrapper, dict):
        return None
    for key in _CLI_RESULT_KEYS:
        value = wrapper.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort: find the first top-level JSON object in ``text``."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None
