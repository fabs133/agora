"""query_ollama_digest — model identity from /api/tags (L1-A capture)."""

from __future__ import annotations

import io
import json

from agora.observe import jsonl


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _patch_tags(monkeypatch, payload: dict) -> None:
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _FakeResp(payload))


def test_digest_found_strips_ollama_prefix(monkeypatch) -> None:
    _patch_tags(monkeypatch, {"models": [{"name": "gemma4:e4b", "digest": "sha256:beef"}]})
    assert jsonl.query_ollama_digest("ollama/gemma4:e4b") == "sha256:beef"


def test_digest_unknown_when_model_absent(monkeypatch) -> None:
    _patch_tags(monkeypatch, {"models": [{"name": "other:1", "digest": "sha256:x"}]})
    assert jsonl.query_ollama_digest("ollama/gemma4:e4b") == "unknown"


def test_digest_unknown_on_daemon_error(monkeypatch) -> None:
    import urllib.request

    def _boom(*_a, **_k):
        raise OSError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert jsonl.query_ollama_digest("ollama/gemma4:e4b") == "unknown"  # telemetry never fails a run
