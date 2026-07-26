"""Document understanding: native text extraction with positional data.

Returns a structure of "units" (page / section / slide / sheet depending on
format). PDF units capture per-word bounding boxes so citation highlighting in
the viewer is possible without re-ingesting. OCR for scanned pages/images is a
documented extension point (needs tesseract/paddle) — not wired in the MVP.
"""
from __future__ import annotations

import csv
import io
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

_MAX_WORD_PAGES = 50  # cap per-word bbox capture to keep understanding JSON bounded


def parse(data: bytes, ext: str) -> dict[str, Any]:
    try:
        if ext == "pdf":
            return _parse_pdf_routed(data)
        if ext == "docx":
            return _parse_docx(data)
        if ext == "pptx":
            return _parse_pptx(data)
        if ext == "xlsx":
            return _parse_xlsx(data)
        if ext == "csv":
            return _parse_csv(data)
        if ext == "txt":
            return _parse_txt(data)
        if ext in ("png", "jpg", "jpeg", "tif", "tiff"):
            return _parse_image(data)
    except Exception as e:  # noqa: BLE001
        log.error("parse_failed", ext=ext, error=str(e))
        return {"anchor_type": "page", "text": "", "units": [], "warning": f"parse error: {e}"}
    return {"anchor_type": "page", "text": "", "units": []}


def _assemble(anchor_type: str, units: list[dict]) -> dict:
    text = "\n\n".join(u.get("text", "") for u in units if u.get("text"))
    return {"anchor_type": anchor_type, "text": text, "units": units}


def _parse_pdf_routed(data: bytes) -> dict:
    """PDFs go to docling-serve (layout + bboxes + OCR); pdfplumber is the fallback."""
    from app.core.config import settings

    if settings.pdf_parser == "docling":
        try:
            from app.services.docling_parser import parse_pdf_docling

            return parse_pdf_docling(data)
        except Exception as e:  # noqa: BLE001
            log.warning("docling_failed_fallback_pdfplumber", error=str(e)[:300])
    result = _parse_pdf(data)
    result["parser"] = "pdfplumber"
    result["ocr_used"] = False
    return result


def _parse_pdf(data: bytes) -> dict:
    import pdfplumber

    units = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            unit = {
                "index": i, "anchor": f"page:{i}", "page": i, "title": None,
                "text": text, "width": float(page.width), "height": float(page.height),
            }
            if i <= _MAX_WORD_PAGES:
                words = []
                for w in page.extract_words():
                    words.append({
                        "t": w["text"],
                        "x0": round(float(w["x0"]), 2), "y0": round(float(w["top"]), 2),
                        "x1": round(float(w["x1"]), 2), "y1": round(float(w["bottom"]), 2),
                    })
                unit["words"] = words
            units.append(unit)
    return _assemble("page", units)


def _parse_docx(data: bytes) -> dict:
    import docx

    doc = docx.Document(io.BytesIO(data))
    units: list[dict] = []
    current = {"index": 1, "anchor": "section:1", "title": None, "text": "", "page": None}
    sec = 1
    for para in doc.paragraphs:
        style = (para.style.name or "").lower() if para.style else ""
        if style.startswith("heading") and para.text.strip():
            if current["text"].strip():
                units.append(current)
                sec += 1
            current = {"index": sec, "anchor": f"section:{sec}", "title": para.text.strip(),
                       "text": para.text + "\n", "page": None}
        else:
            current["text"] += para.text + "\n"
    if current["text"].strip():
        units.append(current)
    # Tables
    for t_i, table in enumerate(doc.tables, start=1):
        rows = ["\t".join(c.text for c in row.cells) for row in table.rows]
        units.append({"index": len(units) + 1, "anchor": f"table:{t_i}", "title": f"Table {t_i}",
                      "text": "\n".join(rows), "page": None})
    return _assemble("section", units or [{"index": 1, "anchor": "section:1", "title": None, "text": "", "page": None}])


def _parse_pptx(data: bytes) -> dict:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    units = []
    for i, slide in enumerate(prs.slides, start=1):
        parts, title = [], None
        for shape in slide.shapes:
            if shape.has_text_frame:
                txt = shape.text_frame.text
                if txt.strip():
                    parts.append(txt)
                    if title is None and shape == slide.shapes.title:
                        title = txt.strip()
        units.append({"index": i, "anchor": f"slide:{i}", "title": title,
                      "text": "\n".join(parts), "page": None})
    return _assemble("slide", units)


def _parse_xlsx(data: bytes) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    units = []
    for i, ws in enumerate(wb.worksheets, start=1):
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append("\t".join("" if c is None else str(c) for c in row))
        units.append({"index": i, "anchor": f"sheet:{ws.title}", "title": ws.title,
                      "text": "\n".join(rows), "page": None})
    return _assemble("sheet", units)


def _parse_csv(data: bytes) -> dict:
    text = data.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = ["\t".join(r) for r in reader]
    return _assemble("sheet", [{"index": 1, "anchor": "sheet:1", "title": None,
                                "text": "\n".join(rows), "page": None}])


def _parse_txt(data: bytes) -> dict:
    text = data.decode("utf-8", errors="replace")
    return _assemble("page", [{"index": 1, "anchor": "page:1", "page": 1, "title": None, "text": text}])


def _parse_image(data: bytes) -> dict:
    # OCR extension point: plug pytesseract / Azure Document Intelligence here.
    return {
        "anchor_type": "page", "text": "", "units": [],
        "warning": "OCR not enabled; image text not extracted (wire tesseract/Azure DI to enable).",
    }
