"""Model + card run profiles — the primary inference-config source.

A :class:`ModelProfile` is a self-contained spec for a run's model
configuration: model id, num_ctx, max_tokens, keep_alive, timeout, plus
Ollama/VRAM sub-sections. Every inference parameter that previously
needed an env var or was hardcoded is expressible here, so a normal
``AGORA_PROFILE=<name> python scripts/run_discord_bot_test.py`` run
needs no other env vars.

Per-field env overrides remain as a secondary escape hatch — see
:func:`apply_env_overrides`. The precedence is:

    env override > profile value > schema default

Loaders look up ``profiles.yaml`` in this order: explicit ``path`` arg,
``AGORA_PROFILES_FILE`` env var, ``./profiles.yaml`` at CWD, then a
packaged default that reproduces today's framework default
(``ollama/qwen2.5:7b-instruct`` with the historical knobs).

The factory built by :func:`build_llm_factory` returns an
``LLMProtocol`` adapter and is what
:func:`agora.plan.harness.build_orchestrator` uses when a profile is
supplied. Legacy (no-profile) callers keep working unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from agora.core.errors import AgoraError
from agora.fleet.llm_adapter import (
    DEFAULT_TIMEOUT_SECONDS,
    LLMProtocol,
    create_llm_adapter,
)


class OllamaProfile(BaseModel):
    """Ollama-daemon-specific knobs that live with the profile."""

    model_config = {"extra": "forbid"}

    base_url: str = "http://localhost:11434"
    num_parallel: int = 1


class VRAMProfile(BaseModel):
    """VRAM-pre-flight knobs that live with the profile."""

    model_config = {"extra": "forbid"}

    safety_margin_mib: int = 1024


class ModelProfile(BaseModel):
    """Complete inference-config spec for one run.

    Any field not set in a YAML profile falls back to the defaults
    encoded here. Typos in field names raise a pydantic validation
    error (``extra='forbid'``) so silent no-ops are impossible.
    """

    model_config = {"extra": "forbid"}

    name: str = ""
    description: str = ""
    model: str
    num_ctx: int | None = 16384
    max_tokens: int = 4096
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    keep_alive: str = "30m"
    ollama: OllamaProfile = Field(default_factory=OllamaProfile)
    vram: VRAMProfile = Field(default_factory=VRAMProfile)


class ProfileSet(BaseModel):
    """A versioned bundle of named profiles plus a default selection."""

    model_config = {"extra": "forbid"}

    version: int = 1
    default: str = ""
    profiles: dict[str, ModelProfile] = Field(default_factory=dict)

    def select(self, name: str = "") -> ModelProfile:
        """Return the named profile, or :attr:`default` when ``name`` is empty.

        Raises :class:`AgoraError` (with the available names) when the name
        is unknown, so a typo'd ``AGORA_PROFILE`` fails loudly rather than
        silently reverting to the default.
        """
        target = name or self.default
        if not target:
            raise AgoraError(
                f"no profile name supplied and no default set; available: {sorted(self.profiles)}"
            )
        prof = self.profiles.get(target)
        if prof is None:
            raise AgoraError(f"unknown profile {target!r}; available: {sorted(self.profiles)}")
        # Stamp the resolved name onto the profile so logs/diagnostics
        # can identify it even when callers passed the empty string.
        if not prof.name:
            return prof.model_copy(update={"name": target})
        return prof


def _packaged_default() -> ProfileSet:
    """Built-in fallback: reproduces today's framework defaults exactly.

    Used when no ``profiles.yaml`` is on disk, so fresh clones and the
    existing test suite keep working without any new config file.
    """
    return ProfileSet(
        version=1,
        default="agora-default",
        profiles={
            "agora-default": ModelProfile(
                name="agora-default",
                description="Built-in fallback (matches pre-profile defaults).",
                model="ollama/qwen2.5:7b-instruct",
                num_ctx=16384,
                max_tokens=4096,
                timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                keep_alive="30m",
                ollama=OllamaProfile(),
                vram=VRAMProfile(safety_margin_mib=512),
            ),
        },
    )


def load_profiles(path: str | Path | None = None) -> ProfileSet:
    """Load a :class:`ProfileSet` from disk, or fall back to the packaged default.

    Precedence:
      1. ``path`` argument when supplied
      2. ``AGORA_PROFILES_FILE`` env var
      3. ``./profiles.yaml`` in the current working directory
      4. Packaged default (so tests and fresh clones work with no file).

    YAML parse errors and pydantic validation failures both surface as
    :class:`AgoraError` with the offending path attached.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        env_path = os.getenv("AGORA_PROFILES_FILE", "").strip()
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(Path.cwd() / "profiles.yaml")

    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise AgoraError(
                f"profiles.yaml at {candidate} could not be parsed as YAML: {exc}"
            ) from exc
        try:
            return ProfileSet.model_validate(raw)
        except ValidationError as exc:
            raise AgoraError(f"profiles.yaml at {candidate} failed validation:\n{exc}") from exc

    return _packaged_default()


# ----------------------------- env overrides ----------------------------------


_BOOL_NONE_LITERALS = {"", "none", "null", "0"}


def _coerce_optional_int(raw: str) -> int | None:
    """``""``/``"none"``/``"null"``/``"0"`` → ``None``, else ``int(raw)``."""
    norm = raw.strip().lower()
    if norm in _BOOL_NONE_LITERALS:
        return None
    return int(raw)


def apply_env_overrides(
    profile: ModelProfile,
    env: Mapping[str, str] | None = None,
) -> ModelProfile:
    """Return a copy of ``profile`` with per-field env overrides applied.

    Pure function — no side effects, no global state. Useful for tests
    that want to probe override behavior with a synthetic env mapping
    rather than mutating ``os.environ``.

    Recognised env vars (each overrides exactly one field):

    - ``AGORA_LLM_MODEL`` → ``model``
    - ``AGORA_LLM_NUM_CTX`` → ``num_ctx``
      (``""``/``"none"``/``"null"``/``"0"`` collapse to ``None`` so callers
      can defer to Ollama's own default)
    - ``AGORA_LLM_MAX_TOKENS`` → ``max_tokens``
    - ``AGORA_LLM_TIMEOUT_SECONDS`` → ``timeout_seconds``
    - ``AGORA_OLLAMA_BASE_URL`` → ``ollama.base_url``
    - ``AGORA_OLLAMA_NUM_PARALLEL`` → ``ollama.num_parallel``
    - ``AGORA_OLLAMA_KEEP_ALIVE`` → ``keep_alive``
    - ``AGORA_VRAM_SAFETY_MARGIN_MIB`` → ``vram.safety_margin_mib``
    """
    src = env if env is not None else os.environ
    updates: dict[str, Any] = {}

    if val := src.get("AGORA_LLM_MODEL"):
        updates["model"] = val
    if "AGORA_LLM_NUM_CTX" in src:
        updates["num_ctx"] = _coerce_optional_int(src["AGORA_LLM_NUM_CTX"])
    if val := src.get("AGORA_LLM_MAX_TOKENS"):
        updates["max_tokens"] = int(val)
    if val := src.get("AGORA_LLM_TIMEOUT_SECONDS"):
        updates["timeout_seconds"] = float(val)
    if val := src.get("AGORA_OLLAMA_KEEP_ALIVE"):
        updates["keep_alive"] = val

    ollama_updates: dict[str, Any] = {}
    if val := src.get("AGORA_OLLAMA_BASE_URL"):
        ollama_updates["base_url"] = val
    if val := src.get("AGORA_OLLAMA_NUM_PARALLEL"):
        ollama_updates["num_parallel"] = int(val)
    if ollama_updates:
        updates["ollama"] = profile.ollama.model_copy(update=ollama_updates)

    vram_updates: dict[str, Any] = {}
    if val := src.get("AGORA_VRAM_SAFETY_MARGIN_MIB"):
        vram_updates["safety_margin_mib"] = int(val)
    if vram_updates:
        updates["vram"] = profile.vram.model_copy(update=vram_updates)

    if not updates:
        return profile
    return profile.model_copy(update=updates)


# ----------------------------- llm factory ------------------------------------


def build_llm_factory(profile: ModelProfile) -> Callable[[str], LLMProtocol]:
    """Return a factory that resolves ``model_ref`` against ``profile``.

    An empty ``model_ref`` (the v2.3 plan-builder emits agents with
    ``model=""`` because the model can't reliably guess a valid id, so
    the harness owns the runtime choice) falls back to ``profile.model``.
    A non-empty ``model_ref`` (per-role override via ``AgentConfig.model``)
    routes through the same per-provider knobs — same base_url, num_ctx,
    keep_alive, max_concurrent, timeout — so a mixed-model run inherits
    the profile's tuning rather than reverting to adapter defaults.

    For ``ollama/*``: passes base_url, num_ctx, max_concurrent
    (=``ollama.num_parallel``), keep_alive, default_max_tokens.

    For ``claude-*`` (direct Anthropic, non-``claude-code/*``): picks up
    ``ANTHROPIC_API_KEY`` from the env, mirroring the historical harness
    closure.

    For any other provider (LiteLLM, claude-code subprocess): just the
    timeout.
    """

    def factory(model_ref: str) -> LLMProtocol:
        chosen = model_ref or profile.model
        kwargs: dict[str, Any] = {"timeout_seconds": profile.timeout_seconds}
        if chosen.startswith("ollama/"):
            kwargs["base_url"] = profile.ollama.base_url
            kwargs["num_ctx"] = profile.num_ctx
            kwargs["max_concurrent"] = profile.ollama.num_parallel
            kwargs["keep_alive"] = profile.keep_alive
            kwargs["default_max_tokens"] = profile.max_tokens
        elif chosen.startswith("claude-") and not chosen.startswith("claude-code/"):
            api_key = os.getenv("ANTHROPIC_API_KEY", "")
            if api_key:
                kwargs["api_key"] = api_key
        return create_llm_adapter(chosen, **kwargs)

    return factory


__all__ = [
    "ModelProfile",
    "OllamaProfile",
    "ProfileSet",
    "VRAMProfile",
    "apply_env_overrides",
    "build_llm_factory",
    "load_profiles",
]
