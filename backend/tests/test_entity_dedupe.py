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


# --------------------------------------------- cross-type facts and false positives
def test_deposit_found_under_another_type_is_a_duplicate():
    # The reported case: the lease type read "Deposit Amount"; the generic contract
    # type's discovery then proposed "Security Deposit Amount" for the same sum.
    known = [("Deposit Amount", "Rs. 60,000/- (Rupees Sixty Thousand Only)"), ("Governing Law", "India")]
    assert _is_duplicate_of_known("Security Deposit Amount", "Rs. 60,000/-", known)


def test_a_short_value_is_not_evidence_of_a_restatement():
    # "3" is inside "rs390", and a quantity says nothing about a stamp duty.
    assert not _values_match("Rs. 390/-", "3")
    assert not _is_duplicate_of_known("Stamp Duty", "Rs. 390/-", [("Quantity", "3")])
    assert not _values_match("Pune", "Pune Municipal Corporation area")   # too short to carry identity


def test_a_word_inside_a_sentence_is_not_a_restatement():
    # "Licensee" appears in the sentence, but they are different facts.
    assert not _values_match("Licensee", "Licensees cannot make alterations without consent")
    assert not _is_duplicate_of_known(
        "Alteration Restrictions", "Licensees cannot make alterations without consent", [("Role", "Licensee")])


def test_a_distinctive_figure_inside_a_longer_value_still_matches():
    assert _values_match("Rs. 60,000/-", "Rs. 60,000/- (Rupees Sixty Thousand Only)")
    assert _values_match("12 March 2026", "Signed on 12 March 2026 at Pune")
    # Without a number, the shorter value must be most of the longer one: a reworded
    # restatement counts, a word that merely appears in a long sentence does not.
    assert _values_match("Residential", "for residential use")
    assert not _values_match("Residential", "Residential use only for the tenant and family members")


def test_matching_known_says_what_it_restates():
    from app.ai.agents.structured import matching_known

    known = [("Governing Law", "India"), ("Deposit Amount", "Rs. 60,000/-")]
    assert matching_known("Security Deposit", "Rs. 60,000/-", known) == ("Deposit Amount", "Rs. 60,000/-")
    assert matching_known("Witness Name", "Anith Shatamraj", known) is None
