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


def _flat_items_tree():
    """furniture: list of {item, quantity}, as flatten_response would produce it."""
    from app.services.fields import flatten_response, validate_schema

    defs = validate_schema([{
        "key": "furniture", "name": "Furniture", "data_type": "list",
        "item": {"key": "furniture_item", "name": "Furniture item", "data_type": "object", "fields": [
            {"key": "item", "name": "Item", "data_type": "string"},
            {"key": "quantity", "name": "Quantity", "data_type": "integer"}]}}])
    payload = {"furniture": [
        {"item": {"value": "Fan", "confidence": 0.95, "quote": "Fan: 4"},
         "quantity": {"value": "4", "confidence": 0.95, "quote": "Fan: 4"}},
        {"item": {"value": "Bulb", "confidence": 0.4, "quote": None},
         "quantity": {"value": "five", "confidence": 0.9, "quote": None}}]}
    return flatten_response(defs, payload)


def test_build_rows_links_parents_and_flags_what_needs_review():
    import uuid

    from app.models.constants import RV_AUTO_ACCEPTED, RV_PENDING
    from app.services.extraction_rows import build_rows, row_id

    ids = dict(tenant_id=uuid.uuid4(), document_id=uuid.uuid4(), doc_type_id=uuid.uuid4())
    built = build_rows(_flat_items_tree(), document_version=1, doc_type_name="Lease", schema_version=3,
                       model="m", prompt_version="p", layout_units=[], autoaccept=0.85, **ids)
    by_path = {f.field_path: f for f in built.fields}
    # Parents come before children, and children point at their parent's deterministic id.
    order = [f.field_path for f in built.fields]
    assert order.index("furniture") < order.index("furniture[0]") < order.index("furniture[0].item")
    assert by_path["furniture[0].item"].parent_id == by_path["furniture[0]"].id
    assert by_path["furniture[0]"].parent_id == by_path["furniture"].id
    assert by_path["furniture"].id == row_id(ids["document_id"], 1, ids["doc_type_id"], "furniture")
    assert all(f.schema_version == 3 for f in built.fields)
    # A confident value is auto-accepted; a low-confidence or un-normalisable one goes to review.
    assert by_path["furniture[0].item"].review_status == RV_AUTO_ACCEPTED
    assert by_path["furniture[1].item"].review_status == RV_PENDING          # confidence 0.4
    assert by_path["furniture[1].quantity"].review_status == RV_PENDING      # "five" isn't an integer
    assert built.leaves == 4 and built.pending == 2 and len(built.reviews) == 2
    assert {r.field_value_id for r in built.reviews} == {
        by_path["furniture[1].item"].id, by_path["furniture[1].quantity"].id}


def test_build_field_def_makes_a_valid_schema_node_for_every_shape():
    import pytest

    from app.services.field_promotion import build_field_def
    from app.services.fields import ensure_keys, validate_schema

    cols = [{"name": "Item", "data_type": "string"}, {"name": "Quantity", "data_type": "integer"},
            {"name": "item", "data_type": "string"}]  # a duplicate name must still get a unique key

    value = build_field_def("deposit", "Deposit", "d", "value", "currency")
    assert value["data_type"] == "currency"
    lst = build_field_def("languages", "Languages", "", "list", "string")
    assert lst["data_type"] == "list" and lst["item"]["data_type"] == "string"
    items = build_field_def("furniture", "Furniture", "", "items", "string", cols)
    assert items["data_type"] == "list" and items["item"]["data_type"] == "object"
    keys = [c["key"] for c in items["item"]["fields"]]
    assert keys == ["item", "quantity", "item_2"]
    group = build_field_def("address", "Address", "", "group", "string", cols[:2])
    assert group["data_type"] == "object" and [c["key"] for c in group["fields"]] == ["item", "quantity"]
    # Each is something the real validator (and so the extractor) accepts.
    validate_schema(ensure_keys([value, lst, items, group]))

    for bad in (dict(shape="items", columns=[]), dict(shape="group", columns=[{"name": " "}]),
                dict(shape="items", columns=[{"name": "A", "data_type": "object"}]),
                dict(shape="table"), dict(shape="value", data_type="object"),
                dict(shape="items", columns=[{"name": f"c{i}"} for i in range(13)])):
        with pytest.raises(ValueError):
            build_field_def("k", "Name", "", bad.pop("shape"), bad.pop("data_type", "string"), bad.pop("columns", None))


def test_infer_shape_recognises_packed_lists():
    from app.ai.field_suggest import infer_shape

    assert infer_shape("Fan: 4, Tube light: 1, Bed: 2")[0] == "items"
    assert infer_shape("5 Fans, 10 Tube lights")[0] == "items"
    assert [c["name"] for c in infer_shape("Fan: 4, Bed: 2")[1]] == ["Item", "Quantity"]
    assert infer_shape("Hindi, English, Marathi")[0] == "list"
    assert infer_shape("Residential purpose only")[0] == "value"
    assert infer_shape("Rs. 14,000, payable monthly")[0] == "value"   # two parts, not a series of entries
    assert infer_shape("")[0] == "value"


def test_suggested_shape_is_validated_and_falls_back_safely():
    from app.ai.field_suggest import Fact, TypeInfo, _clean

    types = [TypeInfo("t1", "Lease", "", [{"key": "license_fee", "name": "License Fee", "data_type": "currency"}])]
    fact = Fact("f1", "Furniture and Appliances", "Fan: 4, Tube light: 1", "", "t1")
    ok = _clean({"shape": "items", "columns": [{"name": "Item", "data_type": "string"},
                                               {"name": "Qty", "data_type": "money"}]}, fact, types)
    assert ok["shape"] == "items"
    assert ok["columns"] == [{"name": "Item", "data_type": "string"}, {"name": "Qty", "data_type": "string"}]
    # A made-up shape, or a table with no usable columns, falls back to what the text itself suggests.
    assert _clean({"shape": "carousel"}, fact, types)["shape"] == "items"
    bare = _clean({"shape": "items", "columns": []}, fact, types)
    assert bare["shape"] == "items" and [c["name"] for c in bare["columns"]] == ["Item", "Quantity"]
    # ...and with nothing to suggest columns from, it becomes a single value rather than an empty table.
    plain = Fact("f2", "Use of Premises", "Residential purpose only", "", "t1")
    assert _clean({"shape": "items", "columns": []}, plain, types)["shape"] == "value"
    # A list or group is never "the same as" an existing single field.
    same = Fact("f3", "License Fee", "Fan: 4, Bed: 2", "", "t1")
    assert _clean({"shape": "items", "columns": [{"name": "Item"}], "existing_key": "license_fee"}, same, types)["existing_key"] is None


def test_empty_containers_mark_a_list_that_was_asked_for_but_found_empty():
    from app.services.extraction_rows import BuiltRows
    from app.services.field_extraction import empty_containers
    from app.services.fields import validate_schema

    defs = validate_schema([
        {"key": "owner", "name": "Owner", "data_type": "string"},
        {"key": "witnesses", "name": "Witnesses", "data_type": "list",
         "item": {"key": "witness", "name": "Witness", "data_type": "string"}},
        {"key": "address", "name": "Address", "data_type": "object",
         "fields": [{"key": "city", "name": "City", "data_type": "string"}]}])
    got = empty_containers(defs, BuiltRows())
    assert [(c.field_path, c.node_kind) for c in got] == [("witnesses", "list"), ("address", "object")]
