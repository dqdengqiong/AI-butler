from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
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
    phone_lookup_secret: str = "development-phone-lookup-secret-change-me"
    phone_encryption_secret: str = "development-phone-encryption-secret-change-me"
    sms_code_secret: str = "development-sms-code-secret-change-me"
    sms_verification_enabled: bool = False
    sms_provider: str = "mock"
    sms_mock_code: str = "123456"
    sms_code_length: int = 6
    sms_code_ttl_seconds: int = 300
    sms_resend_seconds: int = 60
    sms_phone_hourly_limit: int = 5
    sms_device_hourly_limit: int = 10
    sms_max_attempts: int = 5
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
    conversation_topic_idle_seconds: int = 86_400
    conversation_topic_confidence: float = 0.85
    event_retention_days: int = 7
    checkpoint_retention_days: int = 7
    log_level: str = "INFO"
    tracing_enabled: bool = False
    eval_runner_factory: str = ""
    eval_results_path: Path = Path("eval-results/live.json")

    @model_validator(mode="after")
    def validate_auth_security_settings(self) -> Settings:
        """在启动阶段拒绝会削弱手机号加密或破坏挑战约束的配置。"""

        secrets = (
            self.phone_lookup_secret,
            self.phone_encryption_secret,
            self.sms_code_secret,
        )
        if any(len(value) < 32 for value in secrets):
            raise ValueError("phone and sms secrets must contain at least 32 characters")
        if self.sms_provider != "mock":
            raise ValueError("unsupported sms provider")
        if not 4 <= self.sms_code_length <= 8:
            raise ValueError("sms code length must be between 4 and 8")
        if len(self.sms_mock_code) != self.sms_code_length or not self.sms_mock_code.isdigit():
            raise ValueError("mock sms code must match configured numeric length")
        if not 1 <= self.sms_max_attempts <= 5:
            raise ValueError("sms max attempts must be between 1 and 5")
        if not 60 <= self.sms_code_ttl_seconds <= 900:
            raise ValueError("sms code ttl must be between 60 and 900 seconds")
        if not 10 <= self.sms_resend_seconds <= 300:
            raise ValueError("sms resend delay must be between 10 and 300 seconds")
        if self.sms_phone_hourly_limit < 1 or self.sms_device_hourly_limit < 1:
            raise ValueError("sms hourly limits must be positive")
        if self.conversation_topic_idle_seconds < 3600:
            raise ValueError("conversation topic idle threshold must be at least one hour")
        if not 0.5 <= self.conversation_topic_confidence <= 1:
            raise ValueError("conversation topic confidence must be between 0.5 and 1")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
