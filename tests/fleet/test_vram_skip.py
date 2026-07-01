"""AGORA_SKIP_VRAM_CHECK escape-hatch tests (June 30 09:55 — set, but ignored).

preflight_vram (the policy layer in plan/harness) now honours the env var the
README documents. vram.py (the math) stays unaware of it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agora.fleet import vram
from agora.plan import harness


@pytest.fixture(autouse=True)
def _clear_skip(monkeypatch):
    monkeypatch.delenv("AGORA_SKIP_VRAM_CHECK", raising=False)


async def test_skip_set_does_not_call_check_model_fits(monkeypatch) -> None:
    spy = AsyncMock()
    monkeypatch.setattr(harness, "check_model_fits", spy)
    monkeypatch.setenv("AGORA_SKIP_VRAM_CHECK", "1")
    await harness.preflight_vram("ollama/qwen2.5:7b-instruct", "http://localhost:11434")
    spy.assert_not_called()


async def test_unset_runs_the_check(monkeypatch) -> None:
    ok = vram.VRAMCheck(fits=True, free_mib=20000, required_mib=5000, reason="ok")
    spy = AsyncMock(return_value=ok)
    monkeypatch.setattr(harness, "check_model_fits", spy)
    await harness.preflight_vram("ollama/qwen2.5:7b-instruct", "http://localhost:11434")
    spy.assert_awaited_once()


async def test_zero_value_runs_the_check(monkeypatch) -> None:
    ok = vram.VRAMCheck(fits=True, free_mib=20000, required_mib=5000, reason="ok")
    spy = AsyncMock(return_value=ok)
    monkeypatch.setattr(harness, "check_model_fits", spy)
    monkeypatch.setenv("AGORA_SKIP_VRAM_CHECK", "0")
    await harness.preflight_vram("ollama/qwen2.5:7b-instruct", "http://localhost:11434")
    spy.assert_awaited_once()


@pytest.mark.parametrize("value", ["true", "yes", "TRUE", "On", "1"])
async def test_truthy_spellings_skip(monkeypatch, value) -> None:
    spy = AsyncMock()
    monkeypatch.setattr(harness, "check_model_fits", spy)
    monkeypatch.setenv("AGORA_SKIP_VRAM_CHECK", value)
    await harness.preflight_vram("ollama/qwen2.5:7b-instruct", "http://localhost:11434")
    spy.assert_not_called()


def test_env_truthy_helper() -> None:
    import os

    for v in ("1", "true", "yes", "on", "TRUE", "Yes"):
        os.environ["_AGORA_TEST_FLAG"] = v
        assert harness._env_truthy("_AGORA_TEST_FLAG") is True
    for v in ("0", "false", "no", "", "maybe"):
        os.environ["_AGORA_TEST_FLAG"] = v
        assert harness._env_truthy("_AGORA_TEST_FLAG") is False
    del os.environ["_AGORA_TEST_FLAG"]
