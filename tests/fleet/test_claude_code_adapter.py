"""Tests for the Claude Code subprocess adapter.

Never invokes a real ``claude`` binary — ``asyncio.create_subprocess_exec`` is
patched to return a scripted fake process.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agora.core.errors import AgoraError
from agora.fleet.claude_code_adapter import (
    ClaudeCodeSubprocessAdapter,
    _extract_cli_result,
    _extract_json_object,
    _flatten_messages,
)

# ------------------------------------------------------------ construction


def test_missing_binary_raises(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _binary: None)
    with pytest.raises(AgoraError, match="cannot find"):
        ClaudeCodeSubprocessAdapter(binary="claude")


def test_allow_false_refuses_complete(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _b: "/fake/path/claude")
    adapter = ClaudeCodeSubprocessAdapter(allow=False)
    import asyncio

    with pytest.raises(AgoraError, match="disabled"):
        asyncio.run(adapter.complete([{"role": "user", "content": "hi"}]))


# ------------------------------------------------------------ invocation


def _fake_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


async def test_complete_parses_json_response(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _b: "/fake/claude")
    adapter = ClaudeCodeSubprocessAdapter(allow=True)

    payload = b'{"result": "{\\"content\\": \\"hello\\", \\"tool_calls\\": []}"}'
    fake = _fake_proc(stdout=payload)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        resp = await adapter.complete([{"role": "user", "content": "hi"}])
    assert resp.content == "hello"
    assert resp.tool_calls == ()
    assert resp.stop_reason == "end_turn"


async def test_complete_parses_tool_calls(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _b: "/fake/claude")
    adapter = ClaudeCodeSubprocessAdapter(allow=True)

    inner = (
        '{"content": "", "tool_calls": '
        '[{"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}]}'
    )
    wrapped = '{"result": ' + _json_encode(inner) + "}"
    fake = _fake_proc(stdout=wrapped.encode())
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        resp = await adapter.complete(
            [{"role": "user", "content": "make a file"}],
            tools=[{"name": "write_file", "description": "...", "input_schema": {}}],
        )
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "write_file"
    assert resp.tool_calls[0].arguments == {"path": "a.txt", "content": "x"}


async def test_complete_handles_malformed_stdout(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _b: "/fake/claude")
    adapter = ClaudeCodeSubprocessAdapter(allow=True)

    fake = _fake_proc(stdout=b"this is not JSON at all")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        resp = await adapter.complete([{"role": "user", "content": "hi"}])
    # Graceful degradation: raw text becomes content, no tool calls.
    assert "not JSON" in resp.content
    assert resp.tool_calls == ()
    assert resp.stop_reason == "non_json"


async def test_complete_raises_on_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _b: "/fake/claude")
    adapter = ClaudeCodeSubprocessAdapter(allow=True)

    fake = _fake_proc(stdout=b"", stderr=b"boom", returncode=1)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        with pytest.raises(AgoraError, match="exited 1"):
            await adapter.complete([{"role": "user", "content": "hi"}])


async def test_complete_times_out(monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr("shutil.which", lambda _b: "/fake/claude")
    adapter = ClaudeCodeSubprocessAdapter(allow=True, timeout_seconds=0.01)

    async def _hang(*_a: object, **_k: object) -> tuple[bytes, bytes]:
        await asyncio.sleep(1)
        return b"", b""

    proc = MagicMock()
    proc.communicate = _hang
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        with pytest.raises(AgoraError, match="timeout"):
            await adapter.complete([{"role": "user", "content": "hi"}])


# ------------------------------------------------------------ helpers


def test_flatten_messages_renders_tool_blocks() -> None:
    messages = [
        {"role": "user", "content": "please write a file"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "1", "name": "write_file", "input": {"path": "a"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "1", "content": "wrote"}],
        },
    ]
    out = _flatten_messages(messages)
    assert "please write a file" in out
    assert "[tool_use write_file" in out
    assert "[tool_result id=1]" in out


def test_extract_cli_result_finds_result_key() -> None:
    assert _extract_cli_result('{"result": "inner"}') == "inner"
    assert _extract_cli_result('{"response": "inner"}') == "inner"
    assert _extract_cli_result('{"no_match": true}') is None
    assert _extract_cli_result("not json") is None


def test_extract_json_object_strips_code_fences() -> None:
    raw = '```json\n{"content": "x", "tool_calls": []}\n```'
    obj = _extract_json_object(raw)
    assert obj == {"content": "x", "tool_calls": []}


def test_extract_json_object_from_prose() -> None:
    raw = 'some prose {"content": "x", "tool_calls": []} more text'
    obj = _extract_json_object(raw)
    assert obj == {"content": "x", "tool_calls": []}


def _json_encode(s: str) -> str:
    import json

    return json.dumps(s)
