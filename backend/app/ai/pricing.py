"""Rough token→USD cost estimates (per 1M tokens). Update as prices change."""
from __future__ import annotations

# (input_per_1m, output_per_1m)
LLM_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
}

EMBED_PRICES: dict[str, float] = {  # per 1M tokens
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
}


def llm_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    inp, out = LLM_PRICES.get(model, (0.0, 0.0))
    return (prompt_tokens / 1_000_000) * inp + (completion_tokens / 1_000_000) * out


def embed_cost(model: str, tokens: int) -> float:
    return (tokens / 1_000_000) * EMBED_PRICES.get(model, 0.0)
