"""Runtime configuration via env → .env → defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings override-able via AGORA_* env vars or a .env file."""

    model_config = SettingsConfigDict(env_prefix="AGORA_", env_file=".env", extra="ignore")

    # Matrix
    matrix_homeserver: str = "http://localhost:6167"
    matrix_server_name: str = "agora.local"
    matrix_user_id: str = "@agora:agora.local"
    matrix_password: str = ""
    matrix_registration_token: str = "dev_only_CHANGE_ME"

    # LLM (Ollama is the only backend; other backends re-enter via the bench pipeline)
    llm_model: str = "ollama/qwen2.5:7b-instruct"
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

    # Web fetch (fetch_url tool)
    enable_web_fetch: bool = True
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
