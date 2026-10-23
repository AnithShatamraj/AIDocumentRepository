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


def test_summarize_strips_markdown_citations_and_tables():
    from app.services.chat import SUMMARY_CHARS, _summarize

    md = ("Here is the comparison [1]:\n\n| City | Rent |\n|------|------|\n"
          "| Pune | Rs. 14,000 [2] |\n\n**Note:** `Mumbai` differs.")
    out = _summarize(md)
    assert "[1]" not in out and "[2]" not in out
    assert "|" not in out and "--" not in out and "**" not in out and "`" not in out
    assert "Pune" in out and "14,000" in out

    long = _summarize("word " * 200)
    assert len(long) <= SUMMARY_CHARS + 1 and long.endswith("…")


def test_infer_data_type_from_a_value():
    from app.ai.field_suggest import infer_data_type

    assert infer_data_type("Rs. 2152.00/-") == "currency"
    assert infer_data_type("₹30,000") == "currency"
    assert infer_data_type("12") == "integer"
    assert infer_data_type("12.5") == "number"
    assert infer_data_type("31/03/2017") == "date"
    assert infer_data_type("Yes") == "boolean"
    assert infer_data_type("Residential purpose only") == "string"
    assert infer_data_type("") == "string"


def test_suggestions_are_validated_against_the_real_types():
    from app.ai.field_suggest import Fact, TypeInfo, _clean

    types = [TypeInfo("t1", "Lease", "", [{"key": "license_fee", "name": "License Fee", "data_type": "currency"}]),
             TypeInfo("t2", "Contract", "", [])]
    fact = Fact("f1", "License Fee Amount", "Rs. 14000", "", "t1")
    # An invented type id, an invalid data type and a made-up existing key all fall back safely.
    out = _clean({"name": " Monthly   Rent ", "data_type": "money", "document_type_id": "nope",
                  "existing_key": "ghost"}, fact, types)
    assert out["name"] == "Monthly Rent"
    assert out["data_type"] == "currency"          # inferred from the value
    assert out["document_type_id"] == "t1"         # the fact's own type
    assert out["existing_key"] is None
    # ...but a real existing key is kept, and a same-named field is found without the model's help.
    ok = _clean({"name": "License Fee", "document_type_id": "t1", "existing_key": "license_fee"}, fact, types)
    assert ok["existing_key"] == "license_fee"
    assert _clean({"name": "License fee"}, fact, types)["existing_key"] == "license_fee"


def test_unique_key_avoids_collisions():
    from app.services.field_promotion import unique_key

    assert unique_key("Stamp Duty", set()) == "stamp_duty"
    assert unique_key("Stamp Duty", {"stamp_duty"}) == "stamp_duty_2"
    assert unique_key("Stamp Duty", {"stamp_duty", "stamp_duty_2"}) == "stamp_duty_3"
    assert len(unique_key("x" * 80, {"x" * 63})) <= 63
