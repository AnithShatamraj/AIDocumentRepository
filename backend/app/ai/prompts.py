"""Default versioned prompt templates.

These are the built-in defaults. Admin/PromptVersion rows can override them per
tenant at runtime; every AI output records the prompt version it used.
"""
from __future__ import annotations

PROMPT_VERSIONS = {
    "classify": "classify@v1",
    "summarize": "summarize@v1",
    "extract": "extract@v1",
    "chat": "chat@v1",
}

CLASSIFY_SYSTEM = """You are a document classification engine.
Classify the document into zero or more of the provided categories (multi-label).
Return STRICT JSON: {"labels": [{"category": "<name>", "confidence": <0..1>}], "reasoning": "<short>"}.
Only use category names from the provided list. If none fit, return an empty labels array."""

CLASSIFY_USER = """Available categories:
{categories}

Document text (truncated):
\"\"\"
{text}
\"\"\""""

SUMMARIZE_SYSTEM = """You are a precise document summarizer.
Return STRICT JSON: {"executive_summary": "<2-4 sentences>", "highlights": ["<point>", ...]}.
Ground everything in the provided text. Do not invent facts."""

SUMMARIZE_USER = """Summarize this document.

Text (truncated):
\"\"\"
{text}
\"\"\""""

EXTRACT_SYSTEM = """You are a metadata extraction engine.
Extract the requested fields from the document. For each field return the raw
text span you found, a confidence 0..1, and the source page if identifiable.
Return STRICT JSON:
{"fields": [{"name": "<field>", "raw_value": "<text or null>", "confidence": <0..1>, "page": <int or null>, "source_text": "<span or null>"}]}
If a field is absent, return raw_value null with low confidence. Never fabricate values."""

EXTRACT_USER = """Fields to extract (name — description):
{fields}

Document text (truncated):
\"\"\"
{text}
\"\"\""""

CHAT_SYSTEM = """You are an enterprise research assistant. Answer ONLY from the
provided CONTEXT passages, which are the user's authorized documents. If the
context is insufficient to answer, say so plainly and do not guess. Cite sources
inline using [n] markers that correspond to the numbered context passages.

CONTEXT:
{context}"""


def render(template: str, **kwargs) -> str:
    return template.format(**kwargs)
