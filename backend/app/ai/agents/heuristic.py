"""Deterministic, offline ingestion. Keyword classification + regex extraction.

Confidence is deliberately moderate so extracted fields route through the review
queue — which is exactly what you want to demo the human-in-the-loop flow offline.
"""
from __future__ import annotations

import re

from app.ai.agents.schema import (
    ClassificationOutput,
    ExtractionOutput,
    FieldExtraction,
    LabelPrediction,
    SummaryOutput,
)

_CATEGORY_KEYWORDS = {
    "Contracts": [
        "agreement", "party", "parties", "hereby", "shall", "term", "termination",
        "governing law", "effective date", "renewal", "obligations", "whereas",
    ],
    "Invoices": [
        "invoice", "invoice number", "bill to", "total due", "subtotal", "tax",
        "amount due", "vendor", "due date", "payment", "remit",
    ],
}

_DATE_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(r"([$€£]\s?[\d,]+(?:\.\d{2})?|\b[\d,]+\.\d{2}\b)")
_INV_NUM_RE = re.compile(r"invoice\s*(?:no\.?|number|#)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{2,})", re.IGNORECASE)


def classify(text: str, categories: list[str]) -> ClassificationOutput:
    low = text.lower()
    scores: dict[str, int] = {}
    for cat in categories:
        kws = _CATEGORY_KEYWORDS.get(cat, [cat.lower().rstrip("s")])
        scores[cat] = sum(low.count(kw) for kw in kws)
    total = sum(scores.values()) or 1
    labels = []
    for cat, sc in scores.items():
        if sc <= 0:
            continue
        conf = min(0.95, 0.4 + sc / total)
        labels.append(LabelPrediction(category=cat, confidence=round(conf, 3)))
    labels.sort(key=lambda l: l.confidence, reverse=True)
    return ClassificationOutput(
        labels=labels, reasoning="keyword frequency (offline heuristic)", strategy="heuristic"
    )


def summarize(text: str) -> SummaryOutput:
    clean = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    exec_summary = " ".join(sentences[:3])[:600]
    highlights = [s.strip()[:200] for s in sentences[:5] if len(s.strip()) > 20]
    return SummaryOutput(
        executive_summary=exec_summary or clean[:300],
        highlights=highlights[:5],
        strategy="heuristic",
    )


def _find_page(text: str, span: str) -> int | None:
    return None  # page attribution handled upstream when positional data exists


def extract(text: str, fields: list[dict], category: str) -> ExtractionOutput:
    out: list[FieldExtraction] = []
    dates = _DATE_RE.findall(text)
    monies = _MONEY_RE.findall(text)
    for f in fields:
        name = f["name"]
        lname = name.lower()
        raw: str | None = None
        conf = 0.0
        if "invoice number" in lname or lname == "invoice_number":
            m = _INV_NUM_RE.search(text)
            if m:
                raw, conf = m.group(1), 0.7
        elif "date" in lname and dates:
            # first date for effective/invoice date, second for due/expiration when present.
            idx = 1 if ("due" in lname or "expir" in lname) and len(dates) > 1 else 0
            raw, conf = (dates[idx][0] if isinstance(dates[idx], tuple) else dates[idx]), 0.6
        elif any(k in lname for k in ("amount", "value", "total", "tax")) and monies:
            idx = -1 if "total" in lname else 0
            raw, conf = monies[idx], 0.6
        elif "vendor" in lname or "part" in lname:
            first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            if first_line:
                raw, conf = first_line[:120], 0.45
        elif "governing law" in lname:
            m = re.search(r"governing law[^.\n]{0,80}", text, re.IGNORECASE)
            if m:
                raw, conf = m.group(0)[:120], 0.5
        elif "currency" in lname:
            m = re.search(r"[$€£]", text)
            if m:
                raw, conf = {"$": "USD", "€": "EUR", "£": "GBP"}.get(m.group(0), "USD"), 0.55
        out.append(
            FieldExtraction(name=name, raw_value=raw, confidence=round(conf, 3),
                            page=_find_page(text, raw or ""), source_text=raw)
        )
    return ExtractionOutput(fields=out, strategy="heuristic")
