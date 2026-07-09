"""Runtime configuration via env → .env → defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings override-able via AGORA_* env vars or a .env file."""

    model_config = SettingsConfigDict(env_prefix="AGORA_", env_file=".env", extra="ignore")

    # Matrix (the system agent identity + the human observer identity)
    matrix_homeserver: str = "http://localhost:6167"
    matrix_server_name: str = "agora.local"
    matrix_user_id: str = "@agora:agora.local"
    matrix_password: str = ""
    matrix_registration_token: str = "dev_only_CHANGE_ME"
    observer_user: str = "@observer:agora.local"
    observer_password: str = ""

    # LLM (Ollama is the only backend; other backends re-enter via the bench pipeline)
    llm_model: str = "ollama/qwen2.5:7b-instruct"
    #: Named profile to select from ``profiles.yaml`` ("" = the file's default).
    profile: str = ""
    #: Explicit profiles.yaml location ("" = ./profiles.yaml at CWD, then packaged default).
    profiles_file: str = ""
    ollama_base_url: str = "http://localhost:11434"
    llm_timeout_seconds: float = 600.0
    max_parallel_agents: int = 3
    llm_warmup_seconds: float = 600.0
    skip_llm_warmup: bool = False

    # VRAM pre-flight
    skip_vram_check: bool = False
    vram_safety_margin_mib: int = 512

    # Observer
    review_timeout_seconds: float = 86400.0
    watch_rooms: list[str] = Field(default_factory=list)
    enable_observer: bool = True

    # Harness reliability knobs (v3). Flat fields (owner ruling 2B.1) so the env
    # names campaigns emit are preserved verbatim: AGORA_HARNESS_TOOL_ERRORS,
    # AGORA_HARNESS_NUDGE_BUDGET, AGORA_HARNESS_REVIEW_BUDGET,
    # AGORA_HARNESS_SALVAGE_BUDGET, AGORA_ROUTED_RETRY_BUDGET, AGORA_MAX_TASK_RETRIES.
    harness_tool_errors: str = "raw"                 # "raw" | "corrective"
    harness_nudge_budget: int = 0
    harness_review_budget: int = 0
    harness_salvage_budget: int = 0
    routed_retry_budget: int = 2
    max_task_retries: int = 2

    # Web fetch (fetch_url tool) — legacy integration, OFF by default. Only the
    # plan-builder / fastapi-crud flows use it; opt in with AGORA_ENABLE_WEB_FETCH=1.
    enable_web_fetch: bool = False
    fetch_timeout_seconds: float = 30.0
    fetch_max_bytes: int = 1_048_576
    fetch_max_text_bytes: int = 16_384

    # Workspaces
    work_dir: Path = Field(default=Path("./workspace"))
    flows_dir: Path = Field(default=Path("./flows"))
    git_repo_path: Path = Field(default=Path("./workspace/repo"))
    knowledge_cache_dir: Path = Field(default=Path("./workspace/.knowledge"))


def get_settings() -> Settings:
    return Settings()
