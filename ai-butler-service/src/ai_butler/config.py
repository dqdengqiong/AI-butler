from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
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
    search_candidate_results: int = 20
    rag_evidence_max_tokens: int = 4000
    rag_evidence_max_item_tokens: int = 700
    rag_token_safety_margin: int = 512
    rag_embedding_batch_size: int = 32
    rag_vector_upsert_batch_size: int = 128
    rag_max_chunks_per_document: int = 1000
    model_routing_enabled: bool = False
    model_shadow_mode: bool = False
    model_routing_file: Path = Path("model-routing.toml")
    model_api_keys: dict[str, SecretStr] = Field(default_factory=dict)
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
    run_trace_retention_days: int = 30
    memory_audit_retention_days: int = 90
    memory_preference_ttl_days: int = 180
    memory_constraint_ttl_days: int = 365
    log_level: str = "INFO"
    tracing_enabled: bool = False
    eval_runner_factories: dict[str, str] = Field(default_factory=dict)
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
        if not 0 < self.context_soft_limit_ratio < self.context_hard_limit_ratio <= 0.95:
            raise ValueError("context limits must satisfy 0 < soft < hard <= 0.95")
        retention_days = (
            self.event_retention_days,
            self.checkpoint_retention_days,
            self.run_trace_retention_days,
            self.memory_audit_retention_days,
            self.memory_preference_ttl_days,
            self.memory_constraint_ttl_days,
        )
        if any(days < 1 for days in retention_days):
            raise ValueError("retention and memory TTL values must be positive")
        if not 0.5 <= self.conversation_topic_confidence <= 1:
            raise ValueError("conversation topic confidence must be between 0.5 and 1")
        if not 1 <= self.search_max_results <= self.search_candidate_results:
            raise ValueError("search result limits are inconsistent")
        if self.rag_evidence_max_tokens < 256:
            raise ValueError("RAG evidence budget must be at least 256 tokens")
        if not 128 <= self.rag_evidence_max_item_tokens <= self.rag_evidence_max_tokens:
            raise ValueError("RAG item budget must fit inside the evidence budget")
        if not 1 <= self.rag_embedding_batch_size <= 128:
            raise ValueError("RAG embedding batch size must be between 1 and 128")
        if not 1 <= self.rag_vector_upsert_batch_size <= 512:
            raise ValueError("RAG vector batch size must be between 1 and 512")
        if self.rag_max_chunks_per_document < 1:
            raise ValueError("RAG document chunk limit must be positive")
        if self.model_shadow_mode and not self.model_routing_enabled:
            raise ValueError("model shadow mode requires real model routing")
        if self.model_shadow_mode and self.app_env.lower() in {"production", "staging"}:
            raise ValueError("model shadow mode is only allowed in evaluation environments")
        if self.app_env.lower() in {"production", "staging"} and self.search_provider != "tavily":
            raise ValueError("production and staging require the Tavily search provider")
        if self.search_provider == "tavily" and not self.tavily_api_key:
            raise ValueError("Tavily search provider requires an API key")
        if any(not secret.get_secret_value().isascii() for secret in self.model_api_keys.values()):
            raise ValueError("model API keys must contain ASCII characters only")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
