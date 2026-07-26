"""Ingestion facade — selects the strategy (deepagents / direct / heuristic) and
exposes uniform classify / summarize / extract functions to the pipeline."""
from __future__ import annotations

from app.ai.agents import direct, heuristic
from app.ai.agents.schema import ClassificationOutput, ExtractionOutput, SummaryOutput
from app.ai.registry import is_offline
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


def _use_deepagents() -> bool:
    if not settings.ingestion_use_deepagents or is_offline():
        return False
    from app.ai.agents.deepagent import deepagents_available

    return deepagents_available()


def classify_document(text: str, categories: list[str]) -> ClassificationOutput:
    if is_offline():
        return heuristic.classify(text, categories)
    if _use_deepagents():
        try:
            from app.ai.agents.deepagent import agent_classify

            return agent_classify(text, categories)
        except Exception as e:  # noqa: BLE001
            log.warning("deepagents_classify_failed_fallback_direct", error=str(e))
    return direct.classify(text, categories)


def summarize_document(text: str) -> SummaryOutput:
    if is_offline():
        return heuristic.summarize(text)
    try:
        return direct.summarize(text)
    except Exception as e:  # noqa: BLE001
        log.warning("summarize_failed_fallback_heuristic", error=str(e))
        return heuristic.summarize(text)


def extract_fields(text: str, fields: list[dict], category: str) -> ExtractionOutput:
    if is_offline():
        return heuristic.extract(text, fields, category)
    if _use_deepagents():
        try:
            from app.ai.agents.deepagent import agent_extract

            return agent_extract(text, fields, category)
        except Exception as e:  # noqa: BLE001
            log.warning("deepagents_extract_failed_fallback_direct", error=str(e))
    try:
        return direct.extract(text, fields, category)
    except Exception as e:  # noqa: BLE001
        log.warning("extract_failed_fallback_heuristic", error=str(e))
        return heuristic.extract(text, fields, category)
