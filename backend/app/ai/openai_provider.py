"""OpenAI (and OpenAI-compatible) provider."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from app.ai.base import EmbeddingProvider, LLMProvider
from app.ai.pricing import embed_cost, llm_cost
from app.ai.types import EmbeddingResult, LLMResult, Message
from app.core.config import settings


def _client():
    from openai import OpenAI

    kwargs = {"api_key": settings.openai_api_key}
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return OpenAI(**kwargs)


def _aclient():
    from openai import AsyncOpenAI

    kwargs = {"api_key": settings.openai_api_key}
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return AsyncOpenAI(**kwargs)


def _dump(messages: list[Message]) -> list[dict]:
    return [{"role": m.role, "content": m.content} for m in messages]


class OpenAILLM(LLMProvider):
    name = "openai"

    def complete(self, messages, *, model, temperature=0.0, max_tokens=1024) -> LLMResult:
        resp = _client().chat.completions.create(
            model=model, messages=_dump(messages), temperature=temperature, max_tokens=max_tokens
        )
        usage = resp.usage
        pt = usage.prompt_tokens if usage else 0
        ct = usage.completion_tokens if usage else 0
        return LLMResult(
            text=resp.choices[0].message.content or "",
            provider=self.name,
            model=model,
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_usd=llm_cost(model, pt, ct),
        )

    def complete_json(self, messages, *, model, temperature=0.0, max_tokens=2048) -> tuple[dict, LLMResult]:
        resp = _client().chat.completions.create(
            model=model,
            messages=_dump(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        usage = resp.usage
        pt = usage.prompt_tokens if usage else 0
        ct = usage.completion_tokens if usage else 0
        raw = resp.choices[0].message.content or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        result = LLMResult(
            text=raw, provider=self.name, model=model, prompt_tokens=pt, completion_tokens=ct,
            cost_usd=llm_cost(model, pt, ct),
        )
        return parsed, result

    async def astream(self, messages, *, model, temperature=0.2, max_tokens=1024) -> AsyncIterator[str]:
        stream = await _aclient().chat.completions.create(
            model=model, messages=_dump(messages), temperature=temperature,
            max_tokens=max_tokens, stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta


class OpenAIEmbeddings(EmbeddingProvider):
    name = "openai"

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, texts, *, model) -> EmbeddingResult:
        resp = _client().embeddings.create(model=model, input=texts)
        vectors = [d.embedding for d in resp.data]
        tokens = resp.usage.total_tokens if resp.usage else 0
        return EmbeddingResult(
            vectors=vectors, provider=self.name, model=model, dim=len(vectors[0]) if vectors else self.dim,
            tokens=tokens, cost_usd=embed_cost(model, tokens),
        )
