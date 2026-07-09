"""VRAM pre-flight skip: preflight_vram takes an explicit ``skip`` bool.

The env var AGORA_SKIP_VRAM_CHECK still drives it end-to-end — but now via
Settings (2B: env is read only in config.py). The composition root passes
Settings.skip_vram_check into preflight_vram; the policy layer itself is env-free.
vram.py (the math) stays unaware of the flag.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from agora.fleet import vram
from agora.plan import harness


async def test_skip_true_does_not_call_check_model_fits(monkeypatch) -> None:
    spy = AsyncMock()
    monkeypatch.setattr(harness, "check_model_fits", spy)
    await harness.preflight_vram("ollama/qwen2.5:7b-instruct", "http://ollama.test:11434", skip=True)
    spy.assert_not_called()


async def test_skip_false_runs_the_check(monkeypatch) -> None:
    ok = vram.VRAMCheck(fits=True, free_mib=20000, required_mib=5000, reason="ok")
    spy = AsyncMock(return_value=ok)
    monkeypatch.setattr(harness, "check_model_fits", spy)
    await harness.preflight_vram("ollama/qwen2.5:7b-instruct", "http://ollama.test:11434", skip=False)
    spy.assert_awaited_once()


async def test_non_ollama_model_skips_regardless(monkeypatch) -> None:
    spy = AsyncMock()
    monkeypatch.setattr(harness, "check_model_fits", spy)
    await harness.preflight_vram("openai/gpt-4o", "http://unused", skip=False)
    spy.assert_not_called()


def test_settings_reads_skip_vram_check_env(monkeypatch) -> None:
    """AGORA_SKIP_VRAM_CHECK still controls the skip — now parsed by Settings."""
    from agora.config import Settings

    monkeypatch.setenv("AGORA_SKIP_VRAM_CHECK", "1")
    assert Settings().skip_vram_check is True
    monkeypatch.setenv("AGORA_SKIP_VRAM_CHECK", "0")
    assert Settings().skip_vram_check is False
