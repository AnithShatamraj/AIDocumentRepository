"""Provider-agnostic LLM and embedding interfaces."""
from __future__ import annotations

import abc
from collections.abc import AsyncIterator

from app.ai.types import EmbeddingResult, LLMResult, Message


class LLMProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def complete(
        self, messages: list[Message], *, model: str, temperature: float = 0.0, max_tokens: int = 1024
    ) -> LLMResult:
        ...

    @abc.abstractmethod
    def complete_json(
        self, messages: list[Message], *, model: str, temperature: float = 0.0, max_tokens: int = 2048
    ) -> tuple[dict, LLMResult]:
        """Return parsed JSON object plus usage. Providers should coerce to valid JSON."""
        ...

    @abc.abstractmethod
    async def astream(
        self, messages: list[Message], *, model: str, temperature: float = 0.2, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        """Yield text deltas for streaming chat."""
        ...


class EmbeddingProvider(abc.ABC):
    name: str = "base"
    dim: int = 1536

    @abc.abstractmethod
    def embed(self, texts: list[str], *, model: str) -> EmbeddingResult:
        ...
