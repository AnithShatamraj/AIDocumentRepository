"""Direct structured-LLM ingestion (no agent framework). Reliable fallback path."""
from __future__ import annotations

from app.ai import prompts
from app.ai.agents.schema import (
    ClassificationOutput,
    ExtractionOutput,
    FieldExtraction,
    LabelPrediction,
    SummaryOutput,
)
from app.ai.registry import get_llm
from app.ai.types import Message
from app.core.config import settings

_TEXT_LIMIT = 12000  # chars fed to the model


def classify(text: str, categories: list[str]) -> ClassificationOutput:
    llm = get_llm()
    msgs = [
        Message("system", prompts.CLASSIFY_SYSTEM),
        Message("user", prompts.render(prompts.CLASSIFY_USER,
                                       categories="\n".join(f"- {c}" for c in categories),
                                       text=text[:_TEXT_LIMIT])),
    ]
    data, res = llm.complete_json(msgs, model=settings.llm_classify_model)
    labels = [
        LabelPrediction(category=l.get("category", ""), confidence=float(l.get("confidence", 0)))
        for l in data.get("labels", [])
        if l.get("category") in categories
    ]
    return ClassificationOutput(
        labels=labels, reasoning=data.get("reasoning", ""), strategy="direct",
        model=res.model, prompt_version=prompts.PROMPT_VERSIONS["classify"],
        prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens, cost_usd=res.cost_usd,
    )


def summarize(text: str) -> SummaryOutput:
    llm = get_llm()
    msgs = [
        Message("system", prompts.SUMMARIZE_SYSTEM),
        Message("user", prompts.render(prompts.SUMMARIZE_USER, text=text[:_TEXT_LIMIT])),
    ]
    data, res = llm.complete_json(msgs, model=settings.llm_summarize_model)
    return SummaryOutput(
        executive_summary=data.get("executive_summary", ""),
        highlights=list(data.get("highlights", []))[:8],
        strategy="direct", model=res.model, prompt_version=prompts.PROMPT_VERSIONS["summarize"],
        prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens, cost_usd=res.cost_usd,
    )


def extract(text: str, fields: list[dict], category: str) -> ExtractionOutput:
    llm = get_llm()
    field_lines = "\n".join(f"- {f['name']} — {f.get('description', '')}" for f in fields)
    msgs = [
        Message("system", prompts.EXTRACT_SYSTEM),
        Message("user", prompts.render(prompts.EXTRACT_USER, fields=field_lines, text=text[:_TEXT_LIMIT])),
    ]
    data, res = llm.complete_json(msgs, model=settings.llm_extract_model)
    valid_names = {f["name"] for f in fields}
    out = []
    for item in data.get("fields", []):
        name = item.get("name")
        if name not in valid_names:
            continue
        out.append(FieldExtraction(
            name=name, raw_value=item.get("raw_value"),
            confidence=float(item.get("confidence", 0) or 0),
            page=item.get("page"), source_text=item.get("source_text"),
        ))
    return ExtractionOutput(
        fields=out, strategy="direct", model=res.model, prompt_version=prompts.PROMPT_VERSIONS["extract"],
        prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens, cost_usd=res.cost_usd,
    )
