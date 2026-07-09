"""Tests for the profile-aware harness wiring.

Focused on the new ``profile`` kwarg on :func:`build_orchestrator` and the
``safety_margin_mib`` passthrough on :func:`preflight_vram` — both
introduced when ``profiles.yaml`` became the primary inference-config
source. Back-compat for callers that don't pass a profile is covered
separately by the existing fleet/orchestrator tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.fleet.llm_adapter import OllamaAdapter
from agora.fleet.profiles import ModelProfile, OllamaProfile, VRAMProfile
from agora.plan.harness import HarnessConfig, build_orchestrator, preflight_vram
from tests.conftest import FakeMatrixClient


def _cfg(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        work_dir=tmp_path / "ws",
        knowledge_cache_dir=tmp_path / "kb",
    )


def test_build_orchestrator_with_profile_uses_profile_factory(tmp_path) -> None:
    """A profile-built factory must thread base_url, num_ctx, keep_alive."""
    profile = ModelProfile(
        model="ollama/qwen2.5-coder:32b",
        num_ctx=32768,
        max_tokens=8192,
        keep_alive="2h",
        ollama=OllamaProfile(base_url="http://gpu:11434", num_parallel=3),
        vram=VRAMProfile(safety_margin_mib=2048),
    )
    client = FakeMatrixClient()
    orch = build_orchestrator(
        _cfg(tmp_path),
        client,
        "ollama/wrong-this-should-be-ignored",
        profile=profile,
    )
    # profile.ollama.base_url wins over the cfg default.
    assert orch._ollama_base_url == "http://gpu:11434"
    # profile.keep_alive flows into the orchestrator so warmup pinning matches.
    assert orch._keep_alive == "2h"
    # Factory routes through the profile's per-provider knobs.
    adapter = orch._llm_factory("")
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.base_url == "http://gpu:11434"
    assert adapter.num_ctx == 32768
    assert adapter.max_concurrent == 3
    assert adapter.keep_alive == "2h"
    assert adapter.default_max_tokens == 8192


def test_build_orchestrator_without_profile_keeps_legacy_closure(tmp_path) -> None:
    """No profile → existing env-driven closure path, behaviour unchanged."""
    client = FakeMatrixClient()
    cfg = _cfg(tmp_path)
    orch = build_orchestrator(cfg, client, "ollama/qwen2.5:7b-instruct")
    # Legacy default keep_alive when no profile was supplied.
    assert orch._keep_alive == "30m"
    assert orch._ollama_base_url == cfg.ollama_base_url
    # Empty model_ref falls back to the runner-supplied model.
    adapter = orch._llm_factory("")
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.default_model == "qwen2.5:7b-instruct"


def test_preflight_vram_passes_safety_margin_mib(monkeypatch) -> None:
    """safety_margin_mib must be forwarded to check_model_fits."""
    captured: dict = {}

    async def _fake_check(model, base_url, safety_margin_mib):
        captured["safety_margin_mib"] = safety_margin_mib
        captured["model"] = model
        captured["base_url"] = base_url
        from agora.fleet.vram import VRAMCheck

        return VRAMCheck(fits=True, free_mib=100_000, required_mib=20_000, reason="ok (fake)")

    monkeypatch.setattr("agora.plan.harness.check_model_fits", _fake_check)
    import asyncio

    asyncio.run(
        preflight_vram(
            "ollama/qwen2.5-coder:32b",
            "http://localhost:11434",
            safety_margin_mib=2048,
        )
    )
    assert captured["safety_margin_mib"] == 2048
    assert captured["model"] == "ollama/qwen2.5-coder:32b"


def test_preflight_vram_skips_for_non_ollama(monkeypatch) -> None:
    """Non-Ollama models (e.g. openai/*) skip the local VRAM check."""

    async def _explode(*_a, **_k):
        raise AssertionError("check_model_fits must not be called for remote providers")

    monkeypatch.setattr("agora.plan.harness.check_model_fits", _explode)
    import asyncio

    # Returns without raising — safe to skip.
    asyncio.run(preflight_vram("openai/gpt-4o-mini", "http://unused"))


def test_preflight_vram_default_safety_margin_is_512(monkeypatch) -> None:
    """Back-compat: callers that don't supply safety_margin keep the historical 512."""
    captured: dict = {}

    async def _fake_check(model, base_url, safety_margin_mib):
        captured["safety_margin_mib"] = safety_margin_mib
        from agora.fleet.vram import VRAMCheck

        return VRAMCheck(fits=True, free_mib=None, required_mib=0, reason="probe N/A")

    monkeypatch.setattr("agora.plan.harness.check_model_fits", _fake_check)
    import asyncio

    asyncio.run(preflight_vram("ollama/llama3.1", "http://localhost:11434"))
    assert captured["safety_margin_mib"] == 512


@pytest.mark.asyncio
async def test_orchestrator_warmup_uses_profile_keep_alive(tmp_path, monkeypatch) -> None:
    """The Orchestrator's internal warmup call must pin with profile.keep_alive."""
    captured: dict = {}

    async def _fake_warmup(model, *, base_url, deadline_seconds, keep_alive):
        captured["keep_alive"] = keep_alive
        captured["model"] = model

    monkeypatch.setattr("agora.fleet.vram.warmup", _fake_warmup)

    profile = ModelProfile(model="ollama/qwen2.5-coder:32b", keep_alive="2h")
    client = FakeMatrixClient()
    orch = build_orchestrator(_cfg(tmp_path), client, "ollama/qwen2.5-coder:32b", profile=profile)
    # _preflight_models is the only path that triggers warmup; invoke it
    # with a minimal AgentConfig stub.
    from agora.core.agent import AgentConfig
    from agora.core.types import AgentRole

    await orch._preflight_models(
        [
            AgentConfig(
                name="a",
                role=AgentRole.ARCHITECT,
                model="ollama/qwen2.5-coder:32b",
                instructions="",
            )
        ]
    )
    assert captured["keep_alive"] == "2h"


def test_from_env_reads_harness_knobs(monkeypatch) -> None:
    """AGORA_HARNESS_* round-trip into HarnessConfig (v3)."""
    monkeypatch.setenv("AGORA_HARNESS_TOOL_ERRORS", "corrective")
    monkeypatch.setenv("AGORA_HARNESS_NUDGE_BUDGET", "2")
    cfg = HarnessConfig.from_env()
    assert cfg.tool_errors == "corrective"
    assert cfg.nudge_budget == 2


def test_from_env_defaults_are_v2_behavior(monkeypatch) -> None:
    monkeypatch.delenv("AGORA_HARNESS_TOOL_ERRORS", raising=False)
    monkeypatch.delenv("AGORA_HARNESS_NUDGE_BUDGET", raising=False)
    cfg = HarnessConfig.from_env()
    assert cfg.tool_errors == "raw" and cfg.nudge_budget == 0


def test_salvage_budget_default_off_and_env(monkeypatch) -> None:
    """S7: salvage_budget defaults to 0 (construct-nothing) and reads its env."""
    monkeypatch.delenv("AGORA_HARNESS_SALVAGE_BUDGET", raising=False)
    assert HarnessConfig().salvage_budget == 0
    assert HarnessConfig.from_env().salvage_budget == 0
    monkeypatch.setenv("AGORA_HARNESS_SALVAGE_BUDGET", "1")
    assert HarnessConfig.from_env().salvage_budget == 1


def test_from_env_rejects_invalid_tool_errors(monkeypatch) -> None:
    monkeypatch.setenv("AGORA_HARNESS_TOOL_ERRORS", "bogus")
    with pytest.raises(ValueError, match="AGORA_HARNESS_TOOL_ERRORS"):
        HarnessConfig.from_env()
