"""Locate an extracted value's source region in the parsed layout.

Fuzzy-matches the LLM-quoted `source_text` (or raw value) against per-page
layout blocks and returns the region(s) for viewer highlighting.

Scoring is length-aware on purpose. A naive "is either string a substring of
the other" test lets junk blocks win everything: OCR routinely emits 1-2
character fragments (a stray "R" from a stamp), and such a fragment is
contained in almost any quote. So containment is scored by how much of the
quote a block actually accounts for:

  * quote fits inside one block  -> strong match, tightest such block preferred
  * block is a piece of the quote -> worth its share of the quote; pieces on the
    same page are combined, so a passage spanning several blocks highlights in
    full instead of anchoring to one fragment
  * otherwise                     -> character-level similarity

OCR text and LLM quotes rarely match exactly, so below the acceptance threshold
we degrade to a page-level result (rects=[]) rather than draw a wrong box.
"""
from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

ACCEPT_SCORE = 0.55
MIN_BLOCK_CHARS = 4  # ignore OCR noise fragments ("R", "|", "百")
MIN_QUERY_CHARS = 3
PIECE_COVERAGE = 0.04  # a block covering >=4% of the quote counts as part of it
MAX_RECTS = 16
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "")).strip().lower()


def _contains_score(query: str, block_text: str) -> float:
    """Quote fits entirely inside this block — prefer the tightest such block."""
    return 0.95 * (0.9 + 0.1 * (len(query) / max(1, len(block_text))))


def _coverage_score(coverage: float) -> float:
    """Score for blocks that make up a share of the quote (sub-linear so partial
    matches still clear the bar, but tiny fragments score near zero)."""
    return 0.95 * math.sqrt(max(0.0, min(1.0, coverage)))


def locate(units: list[dict], query: str | None, page_hint: int | None = None) -> dict | None:
    """Return {page, rects, page_width, page_height, match} or None."""
    q = _norm(query or "")
    if len(q) < MIN_QUERY_CHARS:
        return _page_only(units, page_hint)

    best: dict | None = None  # {score, unit, rects}

    for unit in units or []:
        blocks = []
        for block in unit.get("blocks") or []:
            bt = _norm(block.get("text") or "")
            if len(bt) < MIN_BLOCK_CHARS or not block.get("bbox"):
                continue
            blocks.append((bt, block))
        if not blocks:
            continue

        page_bonus = 0.05 if (page_hint is not None and unit.get("page") == page_hint) else 0.0

        # --- the whole quote inside a single block ---
        for bt, block in blocks:
            if q in bt:
                score = _contains_score(q, bt) + page_bonus
                if best is None or score > best["score"]:
                    best = {"score": score, "unit": unit, "rects": [block["bbox"]]}

        # --- quote spread across blocks on this page ---
        pieces = [(bt, b) for bt, b in blocks if bt in q and len(bt) / len(q) >= PIECE_COVERAGE]
        if pieces:
            coverage = sum(len(bt) for bt, _ in pieces) / len(q)
            score = _coverage_score(coverage) + page_bonus
            if best is None or score > best["score"]:
                best = {"score": score, "unit": unit, "rects": [b["bbox"] for _, b in pieces][:MAX_RECTS]}

        # --- fuzzy fallback (OCR drift, reworded quotes) ---
        for bt, block in blocks:
            score = SequenceMatcher(None, q[:300], bt[:300]).ratio() + page_bonus
            if best is None or score > best["score"]:
                best = {"score": score, "unit": unit, "rects": [block["bbox"]]}

    if best and best["score"] >= ACCEPT_SCORE:
        unit = best["unit"]
        return {
            "page": unit.get("page"),
            "rects": best["rects"][:MAX_RECTS],
            "page_width": unit.get("width"),
            "page_height": unit.get("height"),
            "match": round(min(best["score"], 1.0), 3),
        }
    return _page_only(units, page_hint)


def locate_any(units: list[dict], queries: list[str | None], page_hint: int | None = None) -> dict | None:
    """Try candidate strings in order; the first that yields a real region wins.

    Models sometimes return a placeholder (e.g. the literal "Page 1") instead of
    a quote, so the extracted value itself is a useful second attempt.
    """
    fallback = None
    for q in queries:
        if not q:
            continue
        loc = locate(units, q, page_hint)
        if loc and loc.get("rects"):
            return loc
        fallback = fallback or loc
    return fallback


def _page_only(units: list[dict], page_hint: int | None) -> dict | None:
    if page_hint is None:
        return None
    for u in units or []:
        if u.get("page") == page_hint:
            return {"page": page_hint, "rects": [], "page_width": u.get("width"),
                    "page_height": u.get("height"), "match": 0.0}
    return None
