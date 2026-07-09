"""Smoke tests for the Typer CLI. Network-dependent subcommands are mocked."""

from __future__ import annotations

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


def test_doctor_exits_nonzero_when_a_check_is_red(monkeypatch) -> None:
    """The CLI wires agora.doctor and exits non-zero on any red (Stage 4)."""
    from agora import doctor

    async def _fake_run_checks(**_kw):
        return [
            doctor.CheckResult("ollama", False, "unreachable at ...", hint="start `ollama serve`"),
            doctor.CheckResult("workspace", True, "git work tree OK"),
        ]

    monkeypatch.setattr(doctor, "run_checks", _fake_run_checks)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "ollama" in result.stdout
    assert "FAIL" in result.stdout


def test_doctor_exits_zero_when_all_green(monkeypatch) -> None:
    from agora import doctor

    async def _fake_run_checks(**_kw):
        return [doctor.CheckResult("ollama", True, "reachable"), doctor.CheckResult("vram", True, "ok")]

    monkeypatch.setattr(doctor, "run_checks", _fake_run_checks)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "all checks passed" in result.stdout


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
