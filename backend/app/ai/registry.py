"""Provider registry — resolves the configured LLM/embedding providers.

Config-driven and multi-provider: `AI_DEFAULT_PROVIDER` selects the LLM, and
each stage can override provider+model via the (future) per-tenant AIConfig.
Falls back to the deterministic `stub` provider whenever credentials are absent,
so the app always runs.
"""
from __future__ import annotations

from functools import lru_cache

from app.ai.base import EmbeddingProvider, LLMProvider
from app.core.config import settings


@lru_cache
def get_llm(provider: str | None = None) -> LLMProvider:
    provider = provider or settings.effective_ai_provider
    if provider == "openai":
        from app.ai.openai_provider import OpenAILLM

        return OpenAILLM()
    if provider == "azure_openai":
        # Azure OpenAI is API-compatible via base_url; reuse OpenAI client.
        from app.ai.openai_provider import OpenAILLM

        return OpenAILLM()
    # anthropic + stub both resolve to stub here until an Anthropic provider is added.
    from app.ai.stub_provider import StubLLM

    return StubLLM()


@lru_cache
def get_embedder(provider: str | None = None) -> EmbeddingProvider:
    provider = provider or settings.effective_embedding_provider
    if provider in ("openai", "azure_openai"):
        from app.ai.openai_provider import OpenAIEmbeddings

        return OpenAIEmbeddings(dim=settings.embedding_dim)
    from app.ai.stub_provider import StubEmbeddings

    return StubEmbeddings(dim=settings.embedding_dim)


def is_offline() -> bool:
    return settings.effective_ai_provider == "stub"
