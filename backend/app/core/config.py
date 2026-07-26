"""Application configuration.

All settings are environment-driven (12-factor). Values default to a working
local setup so the stack boots with zero configuration; production overrides
everything via env / secrets.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Core ---
    environment: Literal["development", "staging", "production", "test"] = "development"
    log_level: str = "INFO"
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 1440
    api_cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # --- Database ---
    postgres_user: str = "aidocs"
    postgres_password: str = "aidocs"
    postgres_db: str = "aidocs"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    # --- Object storage ---
    storage_provider: Literal["minio", "s3", "azure", "gcs"] = "minio"
    storage_bucket: str = "aidocs"
    s3_endpoint_url: str = "http://localhost:9000"
    # Endpoint used when SIGNING presigned URLs — must be resolvable by the
    # browser. In docker-compose the API talks to minio:9000 internally, but
    # browsers need localhost:9000. Empty = same as s3_endpoint_url.
    s3_public_endpoint_url: str = ""
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"
    azure_storage_connection_string: str = ""
    azure_storage_account_url: str = ""
    gcs_project: str = ""

    # --- AI providers ---
    ai_default_provider: Literal["openai", "anthropic", "azure_openai", "stub"] = "openai"
    openai_api_key: str = ""
    openai_base_url: str = ""
    anthropic_api_key: str = ""

    llm_model: str = "gpt-4o-mini"
    llm_classify_model: str = "gpt-4o-mini"
    llm_extract_model: str = "gpt-4o-mini"
    llm_summarize_model: str = "gpt-4o-mini"
    llm_chat_model: str = "gpt-4o-mini"

    embedding_provider: Literal["openai", "azure_openai", "stub"] = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    ingestion_use_deepagents: bool = True

    # --- Document parsing (PDF) ---
    # docling: layout + bboxes + OCR via the external docling-serve service.
    # pdfplumber: legacy in-process fallback (no OCR).
    pdf_parser: Literal["docling", "pdfplumber"] = "docling"
    docling_url: str = "http://localhost:5001"
    docling_ocr: Literal["auto", "force", "off"] = "auto"
    docling_timeout_seconds: int = 1800  # full-page OCR of large scans is slow on CPU

    # --- Pipeline / processing ---
    max_upload_mb: int = 50
    chunk_tokens: int = 800
    chunk_overlap_tokens: int = 120
    classify_confidence_threshold: float = 0.60
    extract_autoaccept_threshold: float = 0.85
    chat_max_tokens_per_day: int = 200_000
    # Minimum top vector similarity for the RAG assistant to consider grounding
    # adequate; below this it refuses. Tuned for real embeddings; the offline stub
    # uses a lower floor (its hashing embeddings compress the similarity range).
    rag_min_similarity: float = 0.28

    # --- Bootstrap seed ---
    seed_tenant_name: str = "Acme Corp"
    # Use a non-reserved TLD: email-validator rejects .test/.example/.localhost.
    seed_admin_email: str = "admin@acme.com"
    seed_admin_password: str = "admin12345"

    # ------------------------------------------------------------------ derived
    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_async(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_sync(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @property
    def effective_ai_provider(self) -> str:
        """Fall back to the deterministic stub when no credentials are present."""
        if self.ai_default_provider == "openai" and not self.openai_api_key:
            return "stub"
        if self.ai_default_provider == "anthropic" and not self.anthropic_api_key:
            return "stub"
        return self.ai_default_provider

    @property
    def effective_embedding_provider(self) -> str:
        if self.embedding_provider == "openai" and not self.openai_api_key:
            return "stub"
        return self.embedding_provider


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
