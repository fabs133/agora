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
from tests.conftest import FakeMatrixClient, make_harness_config


def _cfg(tmp_path: Path) -> HarnessConfig:
    return make_harness_config(work_dir=tmp_path / "ws", knowledge_cache_dir=tmp_path / "kb")


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


def test_from_settings_reads_harness_knobs() -> None:
    """AGORA_HARNESS_* → Settings → HarnessConfig via from_settings (2B: env is
    read only in config.py; harness maps the built Settings object)."""
    from agora.config import Settings

    s = Settings(harness_tool_errors="corrective", harness_nudge_budget=2,
                 matrix_homeserver="http://hs", matrix_password="pw",
                 observer_user="@o:x", ollama_base_url="http://ol")
    cfg = HarnessConfig.from_settings(s)
    assert cfg.tool_errors == "corrective"
    assert cfg.nudge_budget == 2
    assert cfg.homeserver == "http://hs"
    assert cfg.ollama_base_url == "http://ol"


def test_from_settings_defaults_are_v2_behavior() -> None:
    from agora.config import Settings

    cfg = HarnessConfig.from_settings(Settings())
    assert cfg.tool_errors == "raw" and cfg.nudge_budget == 0


def test_salvage_budget_default_off_and_from_settings() -> None:
    """S7: salvage_budget defaults to 0 (construct-nothing) and maps from Settings."""
    from agora.config import Settings

    assert make_harness_config().salvage_budget == 0
    assert HarnessConfig.from_settings(Settings()).salvage_budget == 0
    assert HarnessConfig.from_settings(Settings(harness_salvage_budget=1)).salvage_budget == 1


def test_from_settings_rejects_invalid_tool_errors() -> None:
    from agora.config import Settings

    with pytest.raises(ValueError, match="harness_tool_errors"):
        HarnessConfig.from_settings(Settings(harness_tool_errors="bogus"))


@pytest.mark.asyncio
async def test_build_matrix_client_login_is_bounded(monkeypatch) -> None:
    """A hung homeserver must fail fast and NAMED, never block indefinitely.

    Regression: on 2026-07-15 a dead Conduit hung `run_phased` here for minutes
    with zero output and near-zero CPU — indistinguishable from a model stall.
    The login had no timeout, and the runner's preflight checked Ollama but not
    Conduit, so nothing caught it earlier.
    """
    import asyncio as _asyncio

    from agora.core.errors import AgoraError
    from agora.plan.harness import build_matrix_client

    closed: dict = {"called": False}

    class _HangingClient:
        def __init__(self, **_kw) -> None: ...

        async def login(self, _password: str) -> None:
            await _asyncio.sleep(3600)  # never returns

        async def close(self) -> None:
            closed["called"] = True

    monkeypatch.setattr("agora.plan.harness.AgoraMatrixClient", _HangingClient)
    cfg = make_harness_config()

    with pytest.raises(AgoraError, match="timed out after 8s"):
        await _asyncio.wait_for(build_matrix_client(cfg), timeout=20)

    assert closed["called"], "the half-open session must be closed on timeout"


@pytest.mark.asyncio
async def test_build_matrix_client_requires_a_password() -> None:
    """No password is a named config error, not an opaque auth failure."""
    from dataclasses import replace

    from agora.core.errors import AgoraError
    from agora.plan.harness import build_matrix_client

    cfg = replace(make_harness_config(), system_password="")
    with pytest.raises(AgoraError, match="AGORA_MATRIX_PASSWORD"):
        await build_matrix_client(cfg)
