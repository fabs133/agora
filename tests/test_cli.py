"""Smoke tests for the Typer CLI. Network-dependent subcommands are mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from agora import __version__
from agora.cli import _recommend_model, app

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_recommend_model_tiers() -> None:
    assert _recommend_model(30_000).startswith("ollama/qwen2.5-coder:32b")
    assert _recommend_model(12_000).startswith("ollama/qwen2.5-coder:14b")
    assert _recommend_model(8_000).startswith("ollama/qwen2.5:7b")
    assert _recommend_model(5_000).startswith("ollama/qwen2.5:3b")
    assert "VRAM too low" in _recommend_model(1_000)


def test_doctor_reports_backend_status() -> None:
    """Doctor reports ollama + VRAM without crashing when the service is missing."""
    # Make ollama unreachable and the VRAM probe fail.
    with patch("agora.fleet.vram.probe_free_vram_mib", AsyncMock(return_value=None)), \
         patch("aiohttp.ClientSession") as _session:
        session_instance = MagicMock()
        session_instance.__aenter__ = AsyncMock(return_value=session_instance)
        session_instance.__aexit__ = AsyncMock(return_value=None)
        session_instance.get = MagicMock(side_effect=OSError("unreachable"))
        _session.return_value = session_instance

        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "ollama" in result.stdout
    assert "vram" in result.stdout


def test_build_adapter_routes_ollama(monkeypatch) -> None:
    """Ollama path builds an OllamaAdapter from settings."""
    from agora.cli import _build_adapter
    from agora.config import Settings
    from agora.fleet.llm_adapter import OllamaAdapter

    settings = Settings(
        ollama_base_url="http://localhost:11434",
        llm_timeout_seconds=5.0,
    )
    adapter = _build_adapter("ollama/qwen2.5-coder:7b-instruct", settings)
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.timeout_seconds == 5.0


def test_settings_default_model_is_ollama() -> None:
    """Core regression guard: default LLM is a local Ollama model (no API key)."""
    from agora.config import Settings

    s = Settings()
    assert s.llm_model.startswith("ollama/")
    assert s.max_parallel_agents >= 1
    assert s.llm_timeout_seconds > 0
