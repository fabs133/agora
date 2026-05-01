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
    assert "subscription" in _recommend_model(1_000)


def test_doctor_reports_backend_status() -> None:
    """Doctor reports ollama/claude/API key/VRAM without crashing on missing tools."""
    # Make ollama unreachable, claude missing, and VRAM probe fail.
    with patch("shutil.which", return_value=None), \
         patch("agora.fleet.vram.probe_free_vram_mib", AsyncMock(return_value=None)), \
         patch("aiohttp.ClientSession") as _session:
        session_instance = MagicMock()
        session_instance.__aenter__ = AsyncMock(return_value=session_instance)
        session_instance.__aexit__ = AsyncMock(return_value=None)
        session_instance.get = MagicMock(side_effect=OSError("unreachable"))
        _session.return_value = session_instance

        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "ollama" in result.stdout
    assert "claude" in result.stdout
    assert "vram" in result.stdout


def test_build_adapter_routes_ollama(monkeypatch) -> None:
    """Ollama path doesn't touch API keys or the claude binary."""
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


def test_build_adapter_subscription_path(monkeypatch) -> None:
    from agora.cli import _build_adapter
    from agora.config import Settings
    from agora.fleet.claude_code_adapter import ClaudeCodeSubprocessAdapter

    monkeypatch.setattr("shutil.which", lambda _b: "/fake/claude")
    settings = Settings(allow_claude_subprocess=True)
    adapter = _build_adapter("claude-code/subscription", settings)
    assert isinstance(adapter, ClaudeCodeSubprocessAdapter)


def test_settings_default_model_is_ollama() -> None:
    """Core regression guard: default LLM must not require an API key."""
    from agora.config import Settings

    s = Settings()
    assert s.llm_model.startswith("ollama/")
    assert s.anthropic_api_key == ""
    assert s.allow_claude_subprocess is False
    assert s.max_parallel_agents >= 1
    assert s.llm_timeout_seconds > 0
