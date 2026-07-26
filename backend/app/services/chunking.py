"""Token-window chunking with overlap, preserving page/anchor provenance.

Chunks never cross unit (page/section/slide/sheet) boundaries, so every chunk
carries an exact provenance anchor for citation highlighting.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

_CHUNK_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


@dataclass
class ChunkDraft:
    ordinal: int
    content: str
    page: int | None
    anchor: str | None
    section_title: str | None
    bbox: dict | None = None  # {page, rects[], page_width, page_height}


def _chunk_bbox(unit: dict, piece: str) -> dict | None:
    """Collect bboxes of layout blocks overlapping this chunk (substring heuristic)."""
    blocks = unit.get("blocks") or []
    if not blocks:
        return None
    norm_piece = " ".join(piece.split()).lower()
    rects = []
    for b in blocks:
        bt = " ".join((b.get("text") or "").split()).lower()
        if not bt or not b.get("bbox"):
            continue
        # block inside chunk, or chunk starts/ends inside block
        if bt[:60] in norm_piece or norm_piece[:60] in bt:
            rects.append(b["bbox"])
    if not rects:
        return None
    return {"page": unit.get("page"), "rects": rects[:24],
            "page_width": unit.get("width"), "page_height": unit.get("height")}


def _encoder():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001
        # tiktoken unavailable -> caller uses the character-based fallback.
        return None


def chunk_units(units: list[dict], chunk_tokens: int, overlap_tokens: int) -> list[ChunkDraft]:
    enc = _encoder()
    drafts: list[ChunkDraft] = []
    ordinal = 0
    step = max(1, chunk_tokens - overlap_tokens)

    for unit in units:
        text = (unit.get("text") or "").strip()
        if not text:
            continue
        page = unit.get("page")
        anchor = unit.get("anchor")
        title = unit.get("title")

        if enc is None:
            # Fallback: split by characters (~4 chars/token heuristic).
            size = chunk_tokens * 4
            ov = overlap_tokens * 4
            start = 0
            while start < len(text):
                piece = text[start:start + size]
                drafts.append(ChunkDraft(ordinal, piece, page, anchor, title, _chunk_bbox(unit, piece)))
                ordinal += 1
                start += max(1, size - ov)
            continue

        tokens = enc.encode(text)
        if len(tokens) <= chunk_tokens:
            drafts.append(ChunkDraft(ordinal, text, page, anchor, title, _chunk_bbox(unit, text)))
            ordinal += 1
            continue
        start = 0
        while start < len(tokens):
            window = tokens[start:start + chunk_tokens]
            piece = enc.decode(window).strip()
            if piece:
                drafts.append(ChunkDraft(ordinal, piece, page, anchor, title, _chunk_bbox(unit, piece)))
                ordinal += 1
            start += step
    return drafts


def deterministic_chunk_id(document_id, version: int, ordinal: int, content: str) -> uuid.UUID:
    import hashlib

    h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return uuid.uuid5(_CHUNK_NAMESPACE, f"{document_id}:{version}:{ordinal}:{h}")
