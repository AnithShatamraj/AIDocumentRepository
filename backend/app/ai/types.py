"""Shared AI result types."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    provider: str
    model: str
    dim: int
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class Message:
    role: str  # system | user | assistant
    content: str


@dataclass
class UsageMeter:
    """Accumulates token/cost across a stage or request."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    calls: list = field(default_factory=list)

    def add(self, r: LLMResult | EmbeddingResult) -> None:
        if isinstance(r, LLMResult):
            self.prompt_tokens += r.prompt_tokens
            self.completion_tokens += r.completion_tokens
        else:
            self.prompt_tokens += r.tokens
        self.cost_usd += r.cost_usd
