"""Pure-logic unit tests (no DB / Redis / storage / network)."""
from __future__ import annotations

import datetime as dt

from app.ai.agents import heuristic
from app.ai.stub_provider import StubEmbeddings
from app.services import chunking, mime, normalize
from app.models.constants import VT_CURRENCY, VT_DATE, VT_NUMERIC


def test_normalize_date():
    n = normalize.normalize("03/15/2026", VT_DATE)
    assert n.ok and n.value_date == dt.date(2026, 3, 15)


def test_normalize_currency():
    n = normalize.normalize("$1,166.40", VT_CURRENCY)
    assert n.ok and abs(n.value_number - 1166.40) < 1e-6 and n.value_currency == "USD"


def test_normalize_numeric_failure_flags_not_ok():
    n = normalize.normalize("not a number", VT_NUMERIC)
    assert not n.ok


def test_mime_validation_rejects_extension_mismatch():
    # PNG magic bytes but .pdf extension -> rejected
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    try:
        mime.validate("evil.pdf", png, 10 * 1024 * 1024)
        assert False, "should have rejected"
    except mime.UploadValidationError:
        pass


def test_mime_accepts_real_pdf():
    # A structurally complete PDF ends with an %%EOF trailer.
    data = b"%PDF-1.7\n" + b"x" * 100 + b"\nstartxref\n0\n%%EOF\n"
    ext, m = mime.validate("doc.pdf", data, 10 * 1024 * 1024)
    assert ext == "pdf" and m == "application/pdf"


def test_mime_rejects_truncated_pdf():
    # Regression: a partially uploaded PDF (no %%EOF) must be rejected at the
    # door rather than failing deep inside the parsing stage.
    truncated = b"%PDF-1.7\n" + b"x" * 5000
    try:
        mime.validate("doc.pdf", truncated, 10 * 1024 * 1024)
        assert False, "truncated PDF should be rejected"
    except mime.UploadValidationError as e:
        assert "truncated" in str(e).lower()


def test_chunking_deterministic_ids_and_provenance():
    units = [{"index": 1, "anchor": "page:1", "page": 1, "title": None, "text": "hello world " * 400}]
    drafts = chunking.chunk_units(units, chunk_tokens=50, overlap_tokens=10)
    assert len(drafts) > 1
    assert all(d.page == 1 and d.anchor == "page:1" for d in drafts)
    import uuid
    doc_id = uuid.uuid4()
    id1 = chunking.deterministic_chunk_id(doc_id, 1, 0, drafts[0].content)
    id2 = chunking.deterministic_chunk_id(doc_id, 1, 0, drafts[0].content)
    assert id1 == id2  # idempotent


def test_heuristic_classifies_invoice():
    text = "INVOICE Invoice Number: INV-1 Total Due: $100 Vendor: Acme due date tax subtotal"
    out = heuristic.classify(text, ["Contracts", "Invoices"])
    labels = {l.category for l in out.labels}
    assert "Invoices" in labels


def test_heuristic_extract_invoice_number():
    text = "Invoice Number: INV-2026-00847\nTotal Due: $1,166.40"
    fields = [{"name": "Invoice Number", "type": "text"}, {"name": "Total Amount", "type": "currency"}]
    out = heuristic.extract(text, fields, "Invoices")
    by_name = {f.name: f for f in out.fields}
    assert by_name["Invoice Number"].raw_value == "INV-2026-00847"


def test_stub_embeddings_deterministic_and_similar():
    emb = StubEmbeddings(dim=1536)
    v1 = emb.embed(["the eagle lands at midnight"], model="stub").vectors[0]
    v2 = emb.embed(["the eagle lands at midnight"], model="stub").vectors[0]
    assert v1 == v2  # deterministic
    # Similar text should have higher dot product than unrelated text.
    import math
    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))
    similar = emb.embed(["eagle midnight lands"], model="stub").vectors[0]
    unrelated = emb.embed(["quarterly financial spreadsheet totals"], model="stub").vectors[0]
    assert dot(v1, similar) > dot(v1, unrelated)


def _hit(n: int):
    import uuid

    from app.services.search import Hit

    return Hit(chunk_id=uuid.UUID(int=n), document_id=uuid.UUID(int=0), document_name="d",
               content="", page=1, anchor=None, section_title=None, score=0.0)


def test_interleave_gives_each_query_a_slot_and_dedupes():
    from app.services.chat import _interleave

    main = [_hit(1), _hit(2), _hit(3)]
    pune = [_hit(10), _hit(2)]      # chunk 2 is shared with the main list
    out = [h.chunk_id.int for h in _interleave([main, pune], limit=10)]
    assert out == [1, 10, 2, 3]     # round-robin, no duplicate of 2


def test_interleave_respects_limit():
    from app.services.chat import _interleave

    out = _interleave([[_hit(i) for i in range(1, 6)], [_hit(i) for i in range(6, 11)]], limit=4)
    assert [h.chunk_id.int for h in out] == [1, 6, 2, 7]
