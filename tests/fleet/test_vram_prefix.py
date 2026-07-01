"""Provider-prefix strip tests (June 30 ~10:00 /api/show 404 from the ollama/ prefix)."""

from __future__ import annotations

import pytest

from agora.fleet import vram


@pytest.mark.parametrize("model,expected", [
    ("ollama/qwen2.5:7b-instruct", "qwen2.5:7b-instruct"),
    ("qwen2.5:7b-instruct", "qwen2.5:7b-instruct"),
    ("ollama/gemma4:e4b", "gemma4:e4b"),
])
def test_strip_provider_prefix(model, expected) -> None:
    assert vram._strip_provider_prefix(model) == expected


def test_strip_provider_prefix_rejects_foreign_provider() -> None:
    # Should never be called from a non-ollama path; assert rather than 404.
    with pytest.raises(AssertionError, match="non-ollama"):
        vram._strip_provider_prefix("openai/gpt-4o")


class _ShowResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload or {}

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


class _CapturingSession:
    def __init__(self, response, captured):
        self._response = response
        self._captured = captured

    def post(self, _url, json=None):
        self._captured["body"] = json
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


async def test_get_model_size_strips_prefix_in_show_body(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        "aiohttp.ClientSession",
        lambda **_k: _CapturingSession(
            _ShowResponse(200, {"size": 5 * 1024 * 1024 * 1024}), captured
        ),
    )
    await vram.get_model_size_mib("ollama/foo:bar", "http://localhost:11434")
    # The /api/show POST must carry the Ollama-native name, not the prefixed one.
    assert captured["body"] == {"name": "foo:bar"}
    assert "ollama/" not in captured["body"]["name"]


async def test_get_model_size_404_falls_through_to_heuristic(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        "aiohttp.ClientSession",
        lambda **_k: _CapturingSession(_ShowResponse(404), captured),
    )
    # /api/show 404 (model genuinely unknown to Ollama) → name heuristic.
    size = await vram.get_model_size_mib("ollama/qwen2.5-coder:7b", "http://localhost:11434")
    assert size == 5_000  # 7b heuristic tier
    # And even the 404 request used the stripped name.
    assert captured["body"]["name"] == "qwen2.5-coder:7b"
