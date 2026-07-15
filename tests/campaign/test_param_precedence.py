"""Stage 2B acceptance (d) + (e): config resolution & precedence.

(d) The four inference knobs resolve with the owner's precedence —
    coded default < .env < process env < CAMPAIGN PARAMS — and an
    env-vs-campaign conflict logs a loud line (campaign wins, F19).
(e) Scripts and Settings cannot disagree under env manipulation: every
    endpoint/secret/behavioural constant a script injects is derived from
    the one source (Settings), so the same env yields the same value.
"""

from __future__ import annotations

import importlib

import pytest

from agora.config import env_layer, get_settings, require_secret
from agora.fleet.profiles import ModelProfile
from scripts.run_phased import resolve_effective_params


def _profiles(**knobs) -> dict[str, ModelProfile]:
    return {"m": ModelProfile(model="ollama/x", **knobs)}


# ---------------------------------------------------------------- (d) precedence


def test_profile_default_wins_when_neither_env_nor_campaign() -> None:
    resolved, conflicts = resolve_effective_params(_profiles(num_ctx=16384), None, {})
    assert resolved["m"].num_ctx == 16384
    assert conflicts == []


def test_env_beats_profile_default() -> None:
    resolved, conflicts = resolve_effective_params(
        _profiles(num_ctx=16384), None, {"AGORA_LLM_NUM_CTX": "8192"}
    )
    assert resolved["m"].num_ctx == 8192
    assert conflicts == []


def test_campaign_beats_env_and_emits_conflict_line() -> None:
    resolved, conflicts = resolve_effective_params(
        _profiles(num_ctx=16384),
        {"num_ctx": 4096},
        {"AGORA_LLM_NUM_CTX": "8192"},
    )
    # Campaign wins over env, which wins over the profile default.
    assert resolved["m"].num_ctx == 4096
    assert len(conflicts) == 1
    line = conflicts[0]
    assert "AGORA_LLM_NUM_CTX" in line and "8192" in line and "4096" in line
    assert "campaign wins" in line


def test_campaign_beats_default_no_conflict_when_env_absent() -> None:
    resolved, conflicts = resolve_effective_params(_profiles(seed=42), {"seed": 7}, {})
    assert resolved["m"].seed == 7
    assert conflicts == []


def test_conflict_reported_per_knob_only_for_overlap() -> None:
    # env sets num_ctx + seed; campaign sets seed + temperature. Only seed overlaps.
    resolved, conflicts = resolve_effective_params(
        _profiles(num_ctx=16384, seed=42, temperature=0.0),
        {"seed": 7, "temperature": 0.5},
        {"AGORA_LLM_NUM_CTX": "8192", "AGORA_LLM_SEED": "99"},
    )
    assert resolved["m"].seed == 7  # campaign wins the conflict
    assert resolved["m"].num_ctx == 8192  # env-only knob survives
    assert resolved["m"].temperature == 0.5  # campaign-only knob applies
    assert len(conflicts) == 1 and "AGORA_LLM_SEED" in conflicts[0]


def test_env_layer_process_env_beats_dotenv_beats_absent(monkeypatch, tmp_path) -> None:
    """env_layer(): .env provides values the process env lacks, process env wins
    where both set the same key (matches pydantic-settings' own precedence)."""
    (tmp_path / ".env").write_text(
        "AGORA_LLM_NUM_CTX=1111\nAGORA_LLM_SEED=555\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGORA_LLM_NUM_CTX", "2222")  # process env overrides .env
    monkeypatch.delenv("AGORA_LLM_SEED", raising=False)  # only .env sets seed
    layer = env_layer()
    assert layer["AGORA_LLM_NUM_CTX"] == "2222"  # process env wins
    assert layer["AGORA_LLM_SEED"] == "555"  # .env provides it


# ---------------------------------------------------------------- required secrets


def test_require_secret_returns_value_when_set() -> None:
    assert require_secret("AGORA_MATRIX_PASSWORD", "pw") == "pw"


def test_require_secret_fails_loudly_with_var_name_and_pointer() -> None:
    with pytest.raises(SystemExit) as exc:
        require_secret("AGORA_MATRIX_PASSWORD", "")
    msg = str(exc.value)
    assert "AGORA_MATRIX_PASSWORD" in msg
    assert ".env.example" in msg


# ---------------------------------------------------------------- (e) resolution


def test_settings_is_the_single_source_under_env(monkeypatch) -> None:
    monkeypatch.setenv("AGORA_MATRIX_HOMESERVER", "http://hs.test:6167")
    monkeypatch.setenv("AGORA_OLLAMA_BASE_URL", "http://ol.test:11434")
    monkeypatch.setenv("AGORA_MAX_PARALLEL_AGENTS", "9")
    s = get_settings()
    assert s.matrix_homeserver == "http://hs.test:6167"
    assert s.ollama_base_url == "http://ol.test:11434"
    assert s.max_parallel_agents == 9


def test_script_constants_cannot_disagree_with_settings(monkeypatch) -> None:
    """A composition-root script and Settings resolve identically under the same
    env — the script injects Settings values, it has no independent os.getenv."""
    monkeypatch.setenv("AGORA_MATRIX_HOMESERVER", "http://hs.test:6167")
    monkeypatch.setenv("AGORA_OLLAMA_BASE_URL", "http://ol.test:11434")
    monkeypatch.setenv("AGORA_MATRIX_PASSWORD", "resolved-pass")
    monkeypatch.setenv("AGORA_MAX_PARALLEL_AGENTS", "7")

    import scripts.run_code_review as rcr

    rcr = importlib.reload(rcr)  # re-run its composition-root read under this env
    s = get_settings()
    assert rcr.HOMESERVER == s.matrix_homeserver == "http://hs.test:6167"
    assert rcr.OLLAMA_BASE_URL == s.ollama_base_url == "http://ol.test:11434"
    assert rcr.SYSTEM_PASSWORD == s.matrix_password == "resolved-pass"
    assert rcr.MAX_PARALLEL == s.max_parallel_agents == 7
