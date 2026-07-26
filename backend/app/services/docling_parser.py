"""PDF parsing via the external docling-serve service.

docling-serve (IBM Docling's official API server) runs as its own container/pod
so OCR + layout models scale independently of the Celery workers. This module is
a thin HTTP client: submit the PDF (async convert), poll for the result, then
normalize the DoclingDocument JSON into our `units` structure:

    {"anchor_type": "page", "text": "...", "parser": "docling", "ocr_used": bool,
     "units": [{"index", "anchor", "page", "title", "text", "width", "height",
                "blocks": [{"text", "bbox": {x0,y0,x1,y1}, "type", "order"}]}]}

Bboxes are normalized to TOP-LEFT-origin PDF points at ingestion (Docling emits
bottom-left origin); the viewer scales them by rendered-width / page-width.

Any failure raises DoclingError — the caller falls back to pdfplumber.
"""
from __future__ import annotations

import io
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_API_PREFIXES = ("/v1", "/v1alpha")  # docling-serve moved v1alpha -> v1 across releases


class DoclingError(RuntimeError):
    pass


# ------------------------------------------------------------------ coordinate math
def convert_bbox(bbox: dict, page_height: float) -> dict:
    """Docling bbox {l,t,r,b,coord_origin} -> top-left-origin {x0,y0,x1,y1} points."""
    l, t, r, b = float(bbox["l"]), float(bbox["t"]), float(bbox["r"]), float(bbox["b"])
    origin = (bbox.get("coord_origin") or "BOTTOMLEFT").upper()
    if "BOTTOM" in origin:
        y0, y1 = page_height - t, page_height - b
    else:
        y0, y1 = t, b
    if y0 > y1:
        y0, y1 = y1, y0
    x0, x1 = min(l, r), max(l, r)
    return {"x0": round(x0, 2), "y0": round(y0, 2), "x1": round(x1, 2), "y1": round(y1, 2)}


def pdf_has_text_layer(data: bytes) -> bool:
    """Cheap native-text probe (first 3 pages). Failure => assume scanned."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages[:3]:
            if (page.extract_text() or "").strip():
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ HTTP client
def _submit_and_poll(data: bytes, do_ocr: bool, force_ocr: bool) -> dict:
    base = settings.docling_url.rstrip("/")
    deadline = time.monotonic() + settings.docling_timeout_seconds
    files = {"files": ("document.pdf", data, "application/pdf")}
    form = {
        "to_formats": "json",
        "do_ocr": str(do_ocr).lower(),
        "force_ocr": str(force_ocr).lower(),
        "image_export_mode": "placeholder",
        "table_mode": "fast",
    }

    with httpx.Client(timeout=httpx.Timeout(30.0, read=180.0)) as client:
        # Find a working API prefix + async endpoint; fall back to sync convert.
        task_id, prefix = None, None
        for p in _API_PREFIXES:
            try:
                resp = client.post(f"{base}{p}/convert/file/async", files=files, data=form)
            except httpx.HTTPError as e:
                raise DoclingError(f"docling-serve unreachable at {base}: {e}") from e
            if resp.status_code == 404:
                continue
            if resp.status_code == 422:  # options mismatch across versions — retry minimal
                resp = client.post(f"{base}{p}/convert/file/async",
                                   files={"files": files["files"]}, data={"to_formats": "json"})
            if resp.status_code < 300:
                task_id = resp.json().get("task_id")
                prefix = p
                break
            raise DoclingError(f"docling submit failed: {resp.status_code} {resp.text[:300]}")

        if task_id is None:
            # No async endpoint on this build — one blocking sync convert.
            for p in _API_PREFIXES:
                resp = client.post(
                    f"{base}{p}/convert/file", files=files, data=form,
                    timeout=httpx.Timeout(30.0, read=float(settings.docling_timeout_seconds)),
                )
                if resp.status_code == 404:
                    continue
                if resp.status_code < 300:
                    return resp.json()
                raise DoclingError(f"docling sync convert failed: {resp.status_code} {resp.text[:300]}")
            raise DoclingError("no compatible docling-serve convert endpoint found")

        # Poll the async task until success/failure/timeout.
        while time.monotonic() < deadline:
            st = client.get(f"{base}{prefix}/status/poll/{task_id}", params={"wait": 5})
            st.raise_for_status()
            status = (st.json().get("task_status") or "").lower()
            if status == "success":
                res = client.get(f"{base}{prefix}/result/{task_id}")
                res.raise_for_status()
                return res.json()
            if status in ("failure", "failed", "error"):
                raise DoclingError(f"docling task failed: {st.text[:300]}")
            time.sleep(2)
    raise DoclingError(f"docling timed out after {settings.docling_timeout_seconds}s")


# ------------------------------------------------------------------ normalization
def _table_text(item: dict) -> str:
    try:
        grid = item["data"]["grid"]
        return "\n".join("\t".join((c.get("text") or "") for c in row) for row in grid)
    except Exception:  # noqa: BLE001
        return ""


def _doc_to_units(doc: dict) -> list[dict]:
    pages_meta: dict[int, dict] = {}
    for key, p in (doc.get("pages") or {}).items():
        no = int(p.get("page_no") or key)
        size = p.get("size") or {}
        pages_meta[no] = {"width": float(size.get("width") or 612), "height": float(size.get("height") or 792)}

    blocks_by_page: dict[int, list[dict]] = {}
    order = 0
    items = list(doc.get("texts") or [])
    for t in doc.get("tables") or []:
        t = dict(t)
        t["text"] = _table_text(t)
        t["label"] = "table"
        items.append(t)

    for item in items:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        label = item.get("label") or "text"
        for prov in item.get("prov") or []:
            page_no = int(prov.get("page_no") or 1)
            meta = pages_meta.setdefault(page_no, {"width": 612.0, "height": 792.0})
            bbox = None
            if prov.get("bbox"):
                bbox = convert_bbox(prov["bbox"], meta["height"])
            blocks_by_page.setdefault(page_no, []).append(
                {"text": text, "bbox": bbox, "type": label, "order": order})
            order += 1
            break  # one prov anchor per item is enough for provenance

    units = []
    for page_no in sorted(pages_meta.keys() | blocks_by_page.keys()):
        meta = pages_meta.get(page_no, {"width": 612.0, "height": 792.0})
        blocks = sorted(blocks_by_page.get(page_no, []), key=lambda b: b["order"])
        units.append({
            "index": page_no, "anchor": f"page:{page_no}", "page": page_no, "title": None,
            "text": "\n".join(b["text"] for b in blocks),
            "width": meta["width"], "height": meta["height"],
            "blocks": blocks,
        })
    return units


def _convert(data: bytes, do_ocr: bool, force_ocr: bool) -> list[dict]:
    payload = _submit_and_poll(data, do_ocr=do_ocr, force_ocr=force_ocr)
    doc_json = (payload.get("document") or {}).get("json_content")
    if not doc_json:
        raise DoclingError(f"docling returned no json_content (status={payload.get('status')})")
    return _doc_to_units(doc_json)


def parse_pdf_docling(data: bytes) -> dict:
    mode = settings.docling_ocr
    has_text = pdf_has_text_layer(data)
    started = time.monotonic()

    force = mode == "force"
    units = _convert(data, do_ocr=mode != "off", force_ocr=force)
    text = "\n\n".join(u["text"] for u in units if u["text"])

    # Standard OCR only touches bitmap regions. PDFs with vector/outline content
    # (no text layer, no bitmaps) come back empty — escalate to full-page OCR.
    if not text.strip() and mode == "auto" and not has_text:
        log.info("docling_retry_force_ocr", reason="no_text_from_standard_pass", pages=len(units))
        units = _convert(data, do_ocr=True, force_ocr=True)
        text = "\n\n".join(u["text"] for u in units if u["text"])
        force = True

    ocr_used = force or (mode != "off" and not has_text)
    log.info("docling_parse_ok", pages=len(units), chars=len(text),
             ocr_used=ocr_used, seconds=round(time.monotonic() - started, 1))
    return {
        "anchor_type": "page", "text": text, "units": units,
        "parser": "docling", "ocr_used": bool(ocr_used),
    }
