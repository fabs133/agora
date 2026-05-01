"""Tests for VRAM probe and fit check. No real GPU is required."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agora.core.errors import AgoraError
from agora.fleet import vram


# ------------------------------------------------------------- size heuristics


def test_estimate_model_size_matches_size_tier() -> None:
    assert vram.estimate_model_size_mib("qwen2.5:7b") == 5_000
    assert vram.estimate_model_size_mib("qwen2.5-coder:7b-instruct") == 5_000
    assert vram.estimate_model_size_mib("llama3.3:70b") == 42_000
    assert vram.estimate_model_size_mib("qwen2.5-coder:32b") == 20_000


def test_estimate_model_size_unknown_is_conservative() -> None:
    # Unknown name → conservative default; callers must still probe Ollama.
    assert vram.estimate_model_size_mib("some-proprietary-model") == 8_000


# ------------------------------------------------------------- _run_cmd smoke


async def test_run_cmd_returns_stdout_on_success() -> None:
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"4096\n", b""))
    fake_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        out = await vram._run_cmd("nvidia-smi", "--query-gpu=memory.free")
    assert out == "4096\n"


async def test_run_cmd_none_when_binary_missing() -> None:
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError())):
        assert await vram._run_cmd("not-a-binary") is None


# ------------------------------------------------------------- probes


async def test_probe_nvidia_smi_parses_free_vram() -> None:
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"8192\n", b""))
    fake_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        free = await vram._probe_nvidia_smi()
    assert free == 8192


async def test_probe_nvidia_smi_picks_minimum_across_gpus() -> None:
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"8192\n4096\n", b""))
    fake_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        free = await vram._probe_nvidia_smi()
    assert free == 4096


async def test_probe_free_vram_falls_back_to_none_when_all_fail() -> None:
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError())):
        free = await vram.probe_free_vram_mib()
    assert free is None


# ------------------------------------------------------------- check_model_fits


class _FakeResponse:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status = status
        self._payload = payload or {}

    async def json(self) -> dict:
        return self._payload

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def post(self, *_a: object, **_k: object) -> _FakeResponse:
        return self._response

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None


@pytest.fixture
def patch_ollama_size(monkeypatch):
    """Patch Ollama /api/show to return a scripted size (bytes).

    Also stubs out the `/api/ps` residency probe so existing fit-path tests
    exercise the canonical free-space calculation.
    """

    def _install(size_bytes: int) -> None:
        response = _FakeResponse(status=200, payload={"size": size_bytes})
        monkeypatch.setattr(
            "aiohttp.ClientSession",
            lambda **_k: _FakeSession(response),
        )
        # Force the "not resident" branch so check_model_fits does its real work.
        monkeypatch.setattr(vram, "_is_model_resident", AsyncMock(return_value=False))

    return _install


async def test_fits_when_free_vram_ample(patch_ollama_size, monkeypatch) -> None:
    patch_ollama_size(size_bytes=4 * 1024 * 1024 * 1024)  # 4 GB → ~4915 MiB × 1.2 ≈ 5900
    monkeypatch.setattr(vram, "probe_free_vram_mib", AsyncMock(return_value=16_000))

    check = await vram.check_model_fits("qwen2.5-coder:7b", safety_margin_mib=512)
    assert check.fits is True
    assert check.free_mib == 16_000


async def test_refuses_when_model_exceeds_free_vram(patch_ollama_size, monkeypatch) -> None:
    patch_ollama_size(size_bytes=20 * 1024 * 1024 * 1024)  # 20 GB
    monkeypatch.setattr(vram, "probe_free_vram_mib", AsyncMock(return_value=8_000))

    check = await vram.check_model_fits("qwen2.5-coder:32b", safety_margin_mib=512)
    assert check.fits is False
    assert "needs" in check.reason


async def test_unknown_vram_does_not_block(patch_ollama_size, monkeypatch) -> None:
    patch_ollama_size(size_bytes=5 * 1024 * 1024 * 1024)
    monkeypatch.setattr(vram, "probe_free_vram_mib", AsyncMock(return_value=None))

    check = await vram.check_model_fits("qwen2.5-coder:7b")
    assert check.fits is True
    assert check.free_mib is None
    assert "probe unavailable" in check.reason


async def test_raise_if_wont_fit() -> None:
    ok = vram.VRAMCheck(fits=True, free_mib=8000, required_mib=5000, reason="ok")
    vram.raise_if_wont_fit(ok, "model")  # no-op

    bad = vram.VRAMCheck(fits=False, free_mib=4000, required_mib=20_000, reason="too big")
    with pytest.raises(AgoraError, match="VRAM pre-flight failed"):
        vram.raise_if_wont_fit(bad, "model")


async def test_falls_back_to_heuristic_when_ollama_show_fails(monkeypatch) -> None:
    """If /api/show returns non-200, we use the name heuristic."""
    response = _FakeResponse(status=404)
    monkeypatch.setattr(
        "aiohttp.ClientSession",
        lambda **_k: _FakeSession(response),
    )
    size = await vram.get_model_size_mib("qwen2.5-coder:7b", "http://localhost:11434")
    assert size == 5_000


# ---------------------------------------------------------------- warmup


async def test_warmup_success(monkeypatch) -> None:
    response = _FakeResponse(status=200, payload={"response": "hi"})
    monkeypatch.setattr("aiohttp.ClientSession", lambda **_k: _FakeSession(response))
    # Returns None on success, does not raise.
    await vram.warmup("ollama/qwen2.5-coder:7b", deadline_seconds=5)


async def test_warmup_strips_ollama_prefix(monkeypatch) -> None:
    captured: dict = {}

    class _CapturingResponse(_FakeResponse):
        def __init__(self):
            super().__init__(status=200, payload={"response": "ok"})

    class _CapturingSession(_FakeSession):
        def __init__(self, response):
            super().__init__(response)

        def post(self, _url, json=None):
            captured["payload"] = json
            return self._response

    monkeypatch.setattr(
        "aiohttp.ClientSession", lambda **_k: _CapturingSession(_CapturingResponse())
    )
    await vram.warmup("ollama/qwen2.5-coder:7b-instruct", deadline_seconds=5)
    assert captured["payload"]["model"] == "qwen2.5-coder:7b-instruct"
    assert captured["payload"]["keep_alive"] == "30m"


async def test_warmup_raises_on_http_error(monkeypatch) -> None:
    response = _FakeResponse(status=500, payload={})
    # Need text() for the error path.
    async def _text():
        return "internal error"
    response.text = _text  # type: ignore[attr-defined]

    monkeypatch.setattr("aiohttp.ClientSession", lambda **_k: _FakeSession(response))
    with pytest.raises(AgoraError, match="warm-up HTTP 500"):
        await vram.warmup("ollama/qwen2.5-coder:7b", deadline_seconds=5)


async def test_warmup_raises_on_timeout(monkeypatch) -> None:
    import asyncio

    class _HangingSession:
        def __init__(self, *_a, **_k): ...
        def post(self, *_a, **_k):
            raise asyncio.TimeoutError()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr("aiohttp.ClientSession", _HangingSession)
    with pytest.raises(AgoraError, match="timed out"):
        await vram.warmup("ollama/qwen2.5-coder:7b", deadline_seconds=0.1)


async def test_resident_model_skips_free_vram_check(monkeypatch) -> None:
    """If /api/ps reports the model is already loaded, fits=True regardless of free VRAM."""
    monkeypatch.setattr(vram, "_is_model_resident", AsyncMock(return_value=True))
    # Even with zero free VRAM, a resident model fits.
    monkeypatch.setattr(vram, "probe_free_vram_mib", AsyncMock(return_value=0))
    check = await vram.check_model_fits("ollama/qwen2.5-coder:7b")
    assert check.fits is True
    assert "resident" in check.reason


async def test_warmup_independent_from_llm_timeout(monkeypatch) -> None:
    """A very short `llm_timeout_seconds` is unrelated: warmup uses its own budget."""

    class _SlowResponse(_FakeResponse):
        def __init__(self):
            super().__init__(status=200, payload={"response": "ok"})

        async def json(self):
            import asyncio as _a

            await _a.sleep(0.05)  # simulate a slow load — still within our 5s deadline
            return self._payload

    monkeypatch.setattr("aiohttp.ClientSession", lambda **_k: _FakeSession(_SlowResponse()))
    # Works even with a 5-second deadline — warmup only has to finish within its own budget.
    await vram.warmup("ollama/qwen2.5-coder:7b", deadline_seconds=5)
