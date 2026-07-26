"""Unit tests for Docling bbox normalization, the bbox locator, and chunk bboxes."""
from __future__ import annotations

from app.services import bbox_locator
from app.services.chunking import _chunk_bbox
from app.services.docling_parser import _doc_to_units, convert_bbox


def test_convert_bbox_bottomleft_origin():
    # 792pt page; block spanning y=700..750 from the bottom => 42..92 from the top.
    bbox = {"l": 72, "t": 750, "r": 300, "b": 700, "coord_origin": "BOTTOMLEFT"}
    out = convert_bbox(bbox, page_height=792)
    assert out == {"x0": 72.0, "y0": 42.0, "x1": 300.0, "y1": 92.0}
    assert out["y0"] < out["y1"] and out["x0"] < out["x1"]


def test_convert_bbox_topleft_passthrough():
    bbox = {"l": 10, "t": 20, "r": 110, "b": 60, "coord_origin": "TOPLEFT"}
    out = convert_bbox(bbox, page_height=792)
    assert out == {"x0": 10.0, "y0": 20.0, "x1": 110.0, "y1": 60.0}


def _mk_units():
    return [{
        "index": 1, "anchor": "page:1", "page": 1, "title": None,
        "text": "MASTER SERVICES AGREEMENT\nGoverning Law: State of Delaware\nTotal: $250,000.00",
        "width": 612.0, "height": 792.0,
        "blocks": [
            {"text": "MASTER SERVICES AGREEMENT", "bbox": {"x0": 100, "y0": 50, "x1": 500, "y1": 70}, "type": "section_header", "order": 0},
            {"text": "Governing Law: State of Delaware", "bbox": {"x0": 72, "y0": 300, "x1": 400, "y1": 315}, "type": "text", "order": 1},
            {"text": "Total: $250,000.00", "bbox": {"x0": 72, "y0": 500, "x1": 250, "y1": 515}, "type": "text", "order": 2},
        ],
    }]


def test_locator_exact_substring_match():
    loc = bbox_locator.locate(_mk_units(), "State of Delaware", page_hint=1)
    assert loc is not None and loc["page"] == 1
    assert loc["rects"] == [{"x0": 72, "y0": 300, "x1": 400, "y1": 315}]
    assert loc["match"] >= 0.9


def test_locator_fuzzy_ocr_noise():
    # OCR-ish noise: casing/spacing differences still match the right block.
    loc = bbox_locator.locate(_mk_units(), "governing  law:  state of delaware", page_hint=1)
    assert loc is not None and loc["rects"], "fuzzy match should still locate a region"


def test_locator_degrades_to_page_only():
    loc = bbox_locator.locate(_mk_units(), "text that appears nowhere in the doc at all", page_hint=1)
    assert loc is not None and loc["page"] == 1 and loc["rects"] == []


def test_locator_none_without_page_hint_or_match():
    assert bbox_locator.locate(_mk_units(), "completely unrelated words qqq", page_hint=None) is None


def _units_with_ocr_noise():
    """Real-world shape: OCR emits junk fragments on the cover page while the
    actual text lives on a later page."""
    return [
        {
            "index": 1, "anchor": "page:1", "page": 1, "width": 595.0, "height": 842.0,
            "text": "R Rs.100",
            "blocks": [
                # 1-char OCR fragment — a substring of virtually any query.
                {"text": "R", "bbox": {"x0": 133, "y0": 56, "x1": 467, "y1": 82}, "type": "text", "order": 0},
                {"text": "Rs.100", "bbox": {"x0": 368, "y0": 92, "x1": 514, "y1": 133}, "type": "text", "order": 1},
            ],
        },
        {
            "index": 2, "anchor": "page:2", "page": 2, "width": 595.0, "height": 842.0,
            "text": "Name: Anith Shatamraj Age: 35 Years, Male, PAN: CWBPS2434P",
            "blocks": [
                {"text": "Name: Anith Shatamraj Age: 35 Years, Male, PAN: CWBPS2434P",
                 "bbox": {"x0": 64, "y0": 131, "x1": 533, "y1": 165}, "type": "text", "order": 0},
            ],
        },
    ]


def test_locator_ignores_single_char_ocr_noise():
    """Regression: a 1-char OCR block must not win every field just because it
    is a substring of the quote (it sent every extraction to page 1's header)."""
    units = _units_with_ocr_noise()
    loc = bbox_locator.locate(units, "Name: Anith Shatamraj Age: 35 Years, Male", page_hint=1)
    assert loc is not None
    assert loc["page"] == 2, "should locate the real text on page 2, not the noise on page 1"
    assert loc["rects"] == [{"x0": 64, "y0": 131, "x1": 533, "y1": 165}]


def test_locator_noise_block_alone_degrades_to_page_level():
    units = [_units_with_ocr_noise()[0]]
    loc = bbox_locator.locate(units, "Effective date of this rental agreement", page_hint=1)
    assert loc is not None and loc["rects"] == [], "no real match -> page-level highlight only"


def test_locator_spans_multiple_blocks_for_long_quote():
    units = _mk_units()
    quote = "Governing Law: State of Delaware Total: $250,000.00"
    loc = bbox_locator.locate(units, quote, page_hint=1)
    assert loc is not None and len(loc["rects"]) == 2, "passage across blocks highlights in full"


def test_locate_any_falls_back_to_raw_value():
    """Models sometimes return a placeholder ("Page 1") as the source quote —
    the extracted value itself should still find the region."""
    units = [{
        "index": 1, "anchor": "page:1", "page": 1, "width": 595.0, "height": 842.0, "text": "",
        "blocks": [
            {"text": "2|23b , Dated: 03-05-2023, Rs.1001-",
             "bbox": {"x0": 81, "y0": 341, "x1": 260, "y1": 362}, "type": "text", "order": 0},
        ],
    }]
    assert bbox_locator.locate(units, "Page 1", 1)["rects"] == []  # placeholder finds nothing
    loc = bbox_locator.locate_any(units, ["Page 1", "03-05-2023"], 1)
    assert loc["rects"] == [{"x0": 81, "y0": 341, "x1": 260, "y1": 362}]


def test_locator_prefers_tightest_containing_block():
    units = [{
        "index": 1, "anchor": "page:1", "page": 1, "width": 612.0, "height": 792.0, "text": "",
        "blocks": [
            {"text": "A very long paragraph that mentions Delaware among many other words " * 3,
             "bbox": {"x0": 10, "y0": 10, "x1": 600, "y1": 200}, "type": "text", "order": 0},
            {"text": "Governing law: Delaware",
             "bbox": {"x0": 20, "y0": 300, "x1": 300, "y1": 320}, "type": "text", "order": 1},
        ],
    }]
    loc = bbox_locator.locate(units, "Delaware", page_hint=1)
    assert loc["rects"] == [{"x0": 20, "y0": 300, "x1": 300, "y1": 320}]


def test_doc_to_units_normalization():
    doc = {
        "pages": {"1": {"page_no": 1, "size": {"width": 612, "height": 792}}},
        "texts": [
            {"text": "Hello world", "label": "text",
             "prov": [{"page_no": 1, "bbox": {"l": 10, "t": 782, "r": 200, "b": 762, "coord_origin": "BOTTOMLEFT"}}]},
        ],
        "tables": [],
    }
    units = _doc_to_units(doc)
    assert len(units) == 1
    u = units[0]
    assert u["page"] == 1 and u["width"] == 612 and u["height"] == 792
    assert u["blocks"][0]["bbox"] == {"x0": 10.0, "y0": 10.0, "x1": 200.0, "y1": 30.0}
    assert "Hello world" in u["text"]


def test_chunk_bbox_collects_overlapping_blocks():
    unit = _mk_units()[0]
    piece = "Governing Law: State of Delaware\nTotal: $250,000.00"
    bb = _chunk_bbox(unit, piece)
    assert bb is not None and bb["page"] == 1
    assert len(bb["rects"]) == 2  # both matching blocks captured
