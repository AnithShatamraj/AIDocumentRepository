"""Result shapes for the ingestion stages (provider/strategy independent)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LabelPrediction:
    category: str
    confidence: float


@dataclass
class ClassificationOutput:
    labels: list[LabelPrediction] = field(default_factory=list)
    reasoning: str = ""
    strategy: str = "heuristic"
    model: str = "stub"
    prompt_version: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class SummaryOutput:
    executive_summary: str = ""
    highlights: list[str] = field(default_factory=list)
    strategy: str = "heuristic"
    model: str = "stub"
    prompt_version: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class FieldExtraction:
    name: str
    raw_value: str | None
    confidence: float
    page: int | None = None
    source_text: str | None = None


@dataclass
class ExtractionOutput:
    fields: list[FieldExtraction] = field(default_factory=list)
    strategy: str = "heuristic"
    model: str = "stub"
    prompt_version: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
