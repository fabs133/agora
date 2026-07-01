"""Tests for the model + card run-profile loader and helpers."""

from __future__ import annotations

import pytest

from agora.core.errors import AgoraError
from agora.fleet.llm_adapter import OllamaAdapter
from agora.fleet.profiles import (
    ModelProfile,
    OllamaProfile,
    ProfileSet,
    VRAMProfile,
    apply_env_overrides,
    build_llm_factory,
    load_profiles,
)

# ----------------------------- ModelProfile basics ---------------------------


def test_model_profile_defaults_match_pre_profile_behaviour() -> None:
    prof = ModelProfile(model="ollama/qwen2.5:7b-instruct")
    assert prof.num_ctx == 16384
    assert prof.max_tokens == 4096
    # Campaign sampling baseline: greedy + fixed seed.
    assert prof.temperature == 0.0
    assert prof.seed == 42
    assert prof.keep_alive == "30m"
    assert prof.ollama.base_url == "http://localhost:11434"
    assert prof.ollama.num_parallel == 1
    assert prof.vram.safety_margin_mib == 1024


def test_model_profile_extra_field_forbidden() -> None:
    """Typo'd YAML key must error loudly so silent no-ops are impossible."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelProfile.model_validate({"model": "ollama/x", "num_ctxx": 8192})


def test_ollama_profile_extra_field_forbidden() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OllamaProfile.model_validate({"basurl": "http://x"})


def test_vram_profile_extra_field_forbidden() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VRAMProfile.model_validate({"safety_margin": 1024})


# ----------------------------- ProfileSet.select -----------------------------


def _toy_set() -> ProfileSet:
    return ProfileSet(
        version=1,
        default="a",
        profiles={
            "a": ModelProfile(name="a", model="ollama/llama3.1:8b"),
            "b": ModelProfile(name="b", model="openai/gpt-4o-mini"),
        },
    )


def test_select_default_when_name_empty() -> None:
    chosen = _toy_set().select("")
    assert chosen.name == "a"


def test_select_named_profile() -> None:
    chosen = _toy_set().select("b")
    assert chosen.model == "openai/gpt-4o-mini"


def test_select_unknown_name_lists_available() -> None:
    with pytest.raises(AgoraError) as exc:
        _toy_set().select("nope")
    msg = str(exc.value)
    assert "nope" in msg
    assert "a" in msg and "b" in msg


def test_select_stamps_name_when_blank() -> None:
    s = ProfileSet(
        version=1,
        default="x",
        profiles={"x": ModelProfile(model="ollama/llama3.1")},
    )
    chosen = s.select("x")
    assert chosen.name == "x"


# ----------------------------- load_profiles --------------------------------


def test_load_profiles_packaged_default_when_no_file(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AGORA_PROFILES_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    s = load_profiles()
    # Packaged default reproduces the historical framework default.
    chosen = s.select()
    assert chosen.model == "ollama/qwen2.5:7b-instruct"
    assert chosen.num_ctx == 16384


def test_load_profiles_from_explicit_path(tmp_path) -> None:
    f = tmp_path / "profiles.yaml"
    f.write_text(
        """
version: 1
default: tiny
profiles:
  tiny:
    model: ollama/qwen2.5:3b-instruct
    num_ctx: 8192
    max_tokens: 2048
    keep_alive: 10m
    ollama:
      base_url: http://example:11434
      num_parallel: 2
    vram:
      safety_margin_mib: 512
""",
        encoding="utf-8",
    )
    s = load_profiles(f)
    p = s.select()
    assert p.model == "ollama/qwen2.5:3b-instruct"
    assert p.num_ctx == 8192
    assert p.keep_alive == "10m"
    assert p.ollama.base_url == "http://example:11434"
    assert p.ollama.num_parallel == 2
    assert p.vram.safety_margin_mib == 512


def test_load_profiles_invalid_yaml_raises_agora_error(tmp_path) -> None:
    bad = tmp_path / "profiles.yaml"
    bad.write_text("this: is: not: valid yaml: [", encoding="utf-8")
    with pytest.raises(AgoraError, match="could not be parsed as YAML"):
        load_profiles(bad)


def test_load_profiles_typo_surfaces_as_agora_error(tmp_path) -> None:
    """Pydantic ValidationError on a typo'd key must be wrapped, not bare."""
    bad = tmp_path / "profiles.yaml"
    bad.write_text(
        """
version: 1
default: t
profiles:
  t:
    model: ollama/llama3
    num_ctxx: 8192
""",
        encoding="utf-8",
    )
    with pytest.raises(AgoraError, match="failed validation"):
        load_profiles(bad)


def test_load_profiles_uses_env_var(monkeypatch, tmp_path) -> None:
    f = tmp_path / "custom.yaml"
    f.write_text(
        """
version: 1
default: one
profiles:
  one:
    model: ollama/qwen2.5-coder:14b
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGORA_PROFILES_FILE", str(f))
    monkeypatch.chdir(tmp_path)
    s = load_profiles()
    assert s.select().model == "ollama/qwen2.5-coder:14b"


def test_load_profiles_finds_cwd_file_when_no_arg_or_env(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AGORA_PROFILES_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profiles.yaml").write_text(
        """
version: 1
default: only
profiles:
  only:
    model: ollama/qwen2.5:0.5b
""",
        encoding="utf-8",
    )
    s = load_profiles()
    assert s.select().model == "ollama/qwen2.5:0.5b"


# ----------------------------- apply_env_overrides ---------------------------


def _base_profile() -> ModelProfile:
    return ModelProfile(
        name="base",
        model="ollama/qwen2.5-coder:32b",
        num_ctx=16384,
        max_tokens=4096,
        timeout_seconds=600.0,
        keep_alive="30m",
        ollama=OllamaProfile(base_url="http://localhost:11434", num_parallel=1),
        vram=VRAMProfile(safety_margin_mib=1024),
    )


def test_apply_env_overrides_no_env_is_noop() -> None:
    p = _base_profile()
    out = apply_env_overrides(p, env={})
    assert out is p  # returns the same instance when nothing applies


def test_apply_env_overrides_changes_only_the_overridden_field() -> None:
    p = _base_profile()
    out = apply_env_overrides(p, env={"AGORA_LLM_MAX_TOKENS": "1024"})
    assert out.max_tokens == 1024
    # Everything else identical.
    assert out.model == p.model
    assert out.num_ctx == p.num_ctx
    assert out.keep_alive == p.keep_alive
    assert out.ollama == p.ollama
    assert out.vram == p.vram


def test_apply_env_overrides_handles_num_ctx_none_sentinel() -> None:
    p = _base_profile()
    for sentinel in ("", "none", "null", "0", "NONE", "  "):
        out = apply_env_overrides(p, env={"AGORA_LLM_NUM_CTX": sentinel})
        assert out.num_ctx is None, f"{sentinel!r} should collapse to None"


def test_apply_env_overrides_temperature_and_seed() -> None:
    p = _base_profile()
    out = apply_env_overrides(
        p, env={"AGORA_LLM_TEMPERATURE": "0.7", "AGORA_LLM_SEED": "123"}
    )
    assert out.temperature == 0.7
    assert out.seed == 123


def test_apply_env_overrides_seed_none_sentinel() -> None:
    p = _base_profile()
    for sentinel in ("", "none", "null", "0", "NONE", "  "):
        out = apply_env_overrides(p, env={"AGORA_LLM_SEED": sentinel})
        assert out.seed is None, f"{sentinel!r} should collapse to None"


def test_apply_env_overrides_routes_subsection_fields() -> None:
    p = _base_profile()
    out = apply_env_overrides(
        p,
        env={
            "AGORA_OLLAMA_BASE_URL": "http://remote:11434",
            "AGORA_OLLAMA_NUM_PARALLEL": "4",
            "AGORA_VRAM_SAFETY_MARGIN_MIB": "2048",
        },
    )
    assert out.ollama.base_url == "http://remote:11434"
    assert out.ollama.num_parallel == 4
    assert out.vram.safety_margin_mib == 2048


def test_apply_env_overrides_full_envelope() -> None:
    p = _base_profile()
    out = apply_env_overrides(
        p,
        env={
            "AGORA_LLM_MODEL": "ollama/qwen2.5:7b-instruct",
            "AGORA_LLM_NUM_CTX": "32768",
            "AGORA_LLM_MAX_TOKENS": "8192",
            "AGORA_LLM_TIMEOUT_SECONDS": "300",
            "AGORA_OLLAMA_KEEP_ALIVE": "1h",
        },
    )
    assert out.model == "ollama/qwen2.5:7b-instruct"
    assert out.num_ctx == 32768
    assert out.max_tokens == 8192
    assert out.timeout_seconds == 300.0
    assert out.keep_alive == "1h"


def test_apply_env_overrides_defaults_to_real_os_environ(monkeypatch) -> None:
    monkeypatch.delenv("AGORA_LLM_MODEL", raising=False)
    monkeypatch.setenv("AGORA_LLM_MAX_TOKENS", "2048")
    out = apply_env_overrides(_base_profile())
    assert out.max_tokens == 2048


# ----------------------------- build_llm_factory -----------------------------


def test_build_llm_factory_empty_model_ref_uses_profile_model() -> None:
    p = ModelProfile(
        model="ollama/qwen2.5-coder:14b",
        num_ctx=32768,
        max_tokens=8192,
        keep_alive="45m",
        ollama=OllamaProfile(base_url="http://gpu:11434", num_parallel=2),
    )
    factory = build_llm_factory(p)
    adapter = factory("")  # empty → use profile.model
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.base_url == "http://gpu:11434"
    assert adapter.num_ctx == 32768
    assert adapter.max_concurrent == 2
    assert adapter.keep_alive == "45m"
    assert adapter.default_max_tokens == 8192


def test_build_llm_factory_explicit_ollama_model_routes_through_same_params() -> None:
    p = ModelProfile(
        model="ollama/qwen2.5:7b-instruct",
        num_ctx=8192,
        max_tokens=2048,
        keep_alive="15m",
        ollama=OllamaProfile(base_url="http://h:11434", num_parallel=3),
    )
    factory = build_llm_factory(p)
    # Per-role AgentConfig.model override (still ollama/*) — must inherit
    # the same base_url + num_ctx + keep_alive + max_concurrent.
    adapter = factory("ollama/llama3.1:8b")
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.base_url == "http://h:11434"
    assert adapter.num_ctx == 8192
    assert adapter.max_concurrent == 3
    assert adapter.keep_alive == "15m"
    assert adapter.default_max_tokens == 2048


def test_build_llm_factory_threads_temperature_and_seed() -> None:
    p = ModelProfile(
        model="ollama/qwen2.5-coder:7b", temperature=0.0, seed=42
    )
    adapter = build_llm_factory(p)("")
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.temperature == 0.0
    assert adapter.seed == 42


def test_build_llm_factory_threads_seed_none() -> None:
    p = ModelProfile(model="ollama/qwen2.5-coder:7b", seed=None)
    adapter = build_llm_factory(p)("")
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.seed is None


def test_build_llm_factory_passes_num_ctx_none_through() -> None:
    p = ModelProfile(model="ollama/qwen2.5:7b-instruct", num_ctx=None)
    factory = build_llm_factory(p)
    adapter = factory("")
    assert isinstance(adapter, OllamaAdapter)
    assert adapter.num_ctx is None


def test_build_llm_factory_unknown_model_raises(monkeypatch) -> None:
    """A claude-* model with no API key in env should propagate the existing error."""
    p = ModelProfile(model="claude-haiku-4-5")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    factory = build_llm_factory(p)
    with pytest.raises(AgoraError, match="api_key|subscription|ollama"):
        factory("")
