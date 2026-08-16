"""Discovery-pass deduplication: a dynamically-'discovered' entity must not
duplicate a field the document type's schema already extracted, even when the
model reports it under a different name (a synonym) or as a breakdown of an
existing recurring value (an installment/tiering of the same fact).

These are pure/deterministic tests of the backstop filter — they don't call an
LLM (discover_entities() does, and is intentionally not exercised here).
"""
from __future__ import annotations

from app.ai.agents.structured import _is_duplicate_of_known, _label_similarity, _values_match


# ------------------------------------------------------------ real-world cases
def test_renamed_deposit_is_caught_as_duplicate():
    # The exact case reported: schema already has "Security Deposit"; the
    # discovery pass tried to add the same sum under a different name.
    known = [("Security Deposit", "₹50,000")]
    assert _is_duplicate_of_known("Refundable Deposit Amount", "₹50,000", known)


def test_renamed_deposit_caught_even_with_different_formatting():
    # Same fact, but the discovered value is formatted differently
    # ("Rs. 50000" vs "₹50,000") — value-equality must be format-tolerant.
    known = [("Security Deposit", "₹50,000")]
    assert _is_duplicate_of_known("Advance Paid", "Rs. 50000", known)


def test_rent_installment_with_matching_value_is_caught():
    # The second reported case: "Monthly Rent" already extracted; a
    # time-sliced breakdown that happens to match the same figure should be
    # caught via value equality even though the labels share no vocabulary.
    known = [("Monthly Rent", "₹25,000")]
    assert _is_duplicate_of_known("License Fee for the First 7 Months", "₹25,000", known)


def test_rent_installment_with_different_value_is_not_caught_by_heuristic():
    # Honest limitation: a differently-worded, differently-valued breakdown
    # of the same underlying fact ("License Fee for months 8-19" at a raised
    # rate) shares no label vocabulary and no value with "Monthly Rent" — no
    # deterministic signal exists to catch this. It relies entirely on the
    # model following the prompt's explicit instruction/example, which is why
    # the prompt is grounded with real values. Documenting the boundary here
    # so a future change doesn't assume the heuristic is a complete solution.
    known = [("Monthly Rent", "₹25,000")]
    assert not _is_duplicate_of_known("License Fee for the Next 12 Months", "₹27,500", known)


def test_unrelated_new_entity_is_not_flagged():
    known = [("Security Deposit", "₹50,000"), ("Monthly Rent", "₹25,000")]
    assert not _is_duplicate_of_known("Registration Number", "REG-2024-88231", known)
    assert not _is_duplicate_of_known("Witness Name", "Anith Shatamraj", known)


# ------------------------------------------------------------------- building blocks
def test_label_similarity_ignores_generic_filler_words():
    # Two fields both containing "amount"/"fee" shouldn't look similar on that
    # basis alone — only meaningful shared tokens (e.g. "deposit") should count.
    assert _label_similarity("Refundable Deposit Amount", "Security Deposit") >= 0.5
    assert _label_similarity("Total Amount", "Tax Amount") < 0.5


def test_values_match_is_tolerant_of_currency_formatting():
    assert _values_match("₹50,000", "Rs. 50000")
    assert not _values_match("₹50,000", "₹27,500")
    assert not _values_match(None, "₹50,000")
    assert not _values_match("", "")
