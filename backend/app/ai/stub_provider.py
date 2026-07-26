"""Deterministic, offline provider so the whole app runs with zero API keys.

Embeddings use a hashing vectorizer (bag-of-words hashed into `dim` buckets,
L2-normalized) so lexically similar texts get similar vectors — crude but enough
for vector search to behave sensibly in local dev.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import AsyncIterator

from app.ai.base import EmbeddingProvider, LLMProvider
from app.ai.types import EmbeddingResult, LLMResult, Message

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Dropping common stopwords keeps the hashing embedding focused on content words,
# so offline similarity/refusal reflects meaning rather than function-word overlap.
_STOPWORDS = frozenset(
    "a an the this that these those and or but if then else of to in on at by for with "
    "from as is are was were be been being it its it's do does did done have has had "
    "what which who whom whose when where why how i you he she they we me him her them us "
    "my your his their our not no yes can could should would will shall may might must "
    "about into over under again further once here there all any both each few more most "
    "other some such only own same so than too very s t just".split()
)


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


class StubLLM(LLMProvider):
    name = "stub"

    def complete(self, messages, *, model, temperature=0.0, max_tokens=1024) -> LLMResult:
        last = messages[-1].content if messages else ""
        return LLMResult(
            text=f"[offline stub] {last[:400]}", provider=self.name, model="stub", prompt_tokens=0,
            completion_tokens=0, cost_usd=0.0,
        )

    def complete_json(self, messages, *, model, temperature=0.0, max_tokens=2048) -> tuple[dict, LLMResult]:
        # Structured stub outputs are produced by the heuristic ingestion path,
        # not here; return empty so callers fall back to heuristics.
        return {}, LLMResult(text="{}", provider=self.name, model="stub")

    async def astream(self, messages, *, model, temperature=0.2, max_tokens=1024) -> AsyncIterator[str]:
        # Build a grounded-looking answer from the provided context block.
        context = ""
        question = ""
        for m in messages:
            if m.role == "system" and "CONTEXT" in m.content:
                context = m.content
            if m.role == "user":
                question = m.content
        if not context.strip():
            yield "I couldn't find anything in your authorized documents to answer that."
            return
        snippet = context.split("CONTEXT", 1)[-1].strip()[:600]
        answer = (
            f"(offline stub answer) Based on the retrieved documents, here is what I found "
            f"relevant to \"{question[:120]}\":\n\n{snippet}"
        )
        for word in answer.split(" "):
            yield word + " "


class StubEmbeddings(EmbeddingProvider):
    name = "stub"

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in _tokens(text):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed(self, texts, *, model) -> EmbeddingResult:
        vectors = [self._vec(t) for t in texts]
        return EmbeddingResult(
            vectors=vectors, provider=self.name, model="stub", dim=self.dim, tokens=0, cost_usd=0.0
        )
