"""deepagents-based ingestion agent (classification + metadata extraction).

Uses the `deepagents` framework (LangChain/LangGraph) with structured
`response_format` output — the agent plans/reasons over the document and returns
a typed result. Everything here is defensive: import/availability failures or
agent errors raise, and the calling service falls back to the direct LLM path.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.ai import prompts
from app.ai.agents.schema import (
    ClassificationOutput,
    ExtractionOutput,
    FieldExtraction,
    LabelPrediction,
)
from app.core.config import settings

_TEXT_LIMIT = 12000


def deepagents_available() -> bool:
    if not settings.openai_api_key:
        return False
    try:
        import deepagents  # noqa: F401
        import langchain_openai  # noqa: F401

        return True
    except Exception:
        return False


# --- Structured output schemas (what the agent must return) ---
class _Label(BaseModel):
    category: str
    confidence: float


class _ClassifyResult(BaseModel):
    labels: list[_Label] = Field(default_factory=list)
    reasoning: str = ""


class _FieldValue(BaseModel):
    name: str
    raw_value: str | None = None
    confidence: float = 0.0
    page: int | None = None
    source_text: str | None = None


class _ExtractResult(BaseModel):
    fields: list[_FieldValue] = Field(default_factory=list)


def _model(model_name: str):
    from langchain_openai import ChatOpenAI

    kwargs = {"model": model_name, "api_key": settings.openai_api_key, "temperature": 0}
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return ChatOpenAI(**kwargs)


def _run_agent(model_name: str, system_prompt: str, response_format, text: str):
    from deepagents import create_deep_agent

    agent = create_deep_agent(
        model=_model(model_name), tools=[], system_prompt=system_prompt, response_format=response_format
    )
    result = agent.invoke({"messages": [{"role": "user", "content": f"Document text:\n\n{text[:_TEXT_LIMIT]}"}]})
    parsed = result.get("structured_response")
    if parsed is None:
        raise RuntimeError("deepagent returned no structured_response")
    return parsed


def agent_classify(text: str, categories: list[str]) -> ClassificationOutput:
    system = (
        "You are a document ingestion agent responsible for taxonomy and classification. "
        f"Assign the document to zero or more of the allowed categories: {', '.join(categories)}. "
        "Multi-label is allowed. Provide a confidence in 0..1 for each assigned label. "
        "Only use category names from the allowed list; if none fit, return an empty labels list."
    )
    parsed: _ClassifyResult = _run_agent(settings.llm_classify_model, system, _ClassifyResult, text)
    labels = [
        LabelPrediction(category=l.category, confidence=float(l.confidence))
        for l in parsed.labels
        if l.category in categories
    ]
    return ClassificationOutput(
        labels=labels, reasoning=parsed.reasoning, strategy="deepagents",
        model=settings.llm_classify_model, prompt_version=prompts.PROMPT_VERSIONS["classify"],
    )


def agent_extract(text: str, fields: list[dict], category: str) -> ExtractionOutput:
    field_lines = "\n".join(f"- {f['name']}: {f.get('description', '')}" for f in fields)
    system = (
        f"You extract structured metadata from a {category} document. Extract these fields "
        f"(name: description):\n{field_lines}\n\nReturn one entry per field. Never fabricate "
        "values; if a field is absent, set raw_value to null with low confidence. Provide a "
        "confidence in 0..1 and the source page if identifiable."
    )
    parsed: _ExtractResult = _run_agent(settings.llm_extract_model, system, _ExtractResult, text)
    valid = {f["name"] for f in fields}
    out = [
        FieldExtraction(
            name=i.name, raw_value=i.raw_value, confidence=float(i.confidence or 0),
            page=i.page, source_text=i.source_text,
        )
        for i in parsed.fields
        if i.name in valid
    ]
    return ExtractionOutput(
        fields=out, strategy="deepagents", model=settings.llm_extract_model,
        prompt_version=prompts.PROMPT_VERSIONS["extract"],
    )
