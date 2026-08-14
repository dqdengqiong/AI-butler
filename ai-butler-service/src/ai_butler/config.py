from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_cors_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:5173"])
    auth_access_token_secret: str = "development-access-token-secret-change-me"
    auth_refresh_token_secret: str = "development-refresh-token-secret-change-me"
    auth_access_token_seconds: int = 1800
    auth_refresh_token_seconds: int = 2_592_000
    stream_ticket_secret: str = "development-stream-ticket-secret-change-me"
    stream_ticket_seconds: int = 600
    app_database_url: str = "postgresql+psycopg://butler_app:butler_app@127.0.0.1:5432/butler_dev"
    migration_database_url: str = (
        "postgresql+psycopg://butler_migrator:butler_migrator@127.0.0.1:5432/butler_dev"
    )
    langgraph_database_url: str = (
        "postgresql://butler_app:butler_app@127.0.0.1:5432/butler_langgraph_dev"
    )
    langgraph_migration_database_url: str = (
        "postgresql://butler_migrator:butler_migrator@127.0.0.1:5432/butler_langgraph_dev"
    )
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection: str = "ai_butler_knowledge"
    search_provider: str = "fake"
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"
    search_timeout_seconds: float = 15.0
    search_max_queries: int = 3
    search_max_results: int = 5
    llm_provider: str = "fake"
    llm_base_url: str = ""
    llm_api_key: str = ""
    chat_model: str = "fake-chat-v1"
    embedding_model: str = "fake-embedding-v1"
    embedding_dimensions: int = 8
    wechat_auth_mode: str = "mock"
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    object_storage_backend: str = "local"
    object_storage_local_path: Path = Path("local-storage")
    public_base_url: str = "http://127.0.0.1:8000"
    official_source_domains: list[str] = Field(default_factory=list)
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_region: str = "auto"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    sse_heartbeat_seconds: int = 15
    sse_poll_interval_ms: int = 250
    worker_poll_interval_ms: int = 500
    worker_lease_seconds: int = 600
    context_window_tokens: int = 16_000
    context_soft_limit_ratio: float = 0.70
    context_hard_limit_ratio: float = 0.85
    event_retention_days: int = 7
    checkpoint_retention_days: int = 7
    log_level: str = "INFO"
    tracing_enabled: bool = False
    eval_runner_factory: str = ""
    eval_results_path: Path = Path("eval-results/live.json")


@lru_cache
def get_settings() -> Settings:
    return Settings()
