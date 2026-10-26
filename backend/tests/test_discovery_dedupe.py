"""Cleanup of discovered ("new") fields that restate something already read."""
from __future__ import annotations

from app.models.constants import RV_CORRECTED, RV_PENDING, RV_REJECTED, RV_VERIFIED
from app.services.discovery_dedupe import Candidate, plan_redundant

KNOWN = [("Deposit Amount", "Rs. 60,000/- (Rupees Sixty Thousand Only)"), ("Use of Premises", "for residential use")]


def _c(id_, name, value, status=RV_PENDING, conf=0.9):
    return Candidate(id_, name, value, status, conf)


def test_removes_a_suggestion_that_restates_an_extracted_field():
    plan = plan_redundant([_c(1, "Security Deposit Amount", "Rs. 60,000/-", RV_REJECTED)], KNOWN)
    assert [p.candidate.id for p in plan] == [1]
    assert plan[0].restates[0] == "Deposit Amount"


def test_keeps_genuinely_new_facts():
    assert plan_redundant([_c(1, "Pets Allowed", "Not permitted"), _c(2, "Lock-in Period", "3 months")], KNOWN) == []


def test_never_removes_something_a_person_accepted_and_counts_it_as_known():
    verified = _c(1, "Stamp Duty", "Rs. 390/-", RV_VERIFIED)
    corrected = _c(2, "Registration Fee", "Rs. 1000/-", RV_CORRECTED)
    dup_of_verified = _c(3, "Stamp Duty Paid", "Rs. 390/-", RV_PENDING)
    plan = plan_redundant([verified, corrected, dup_of_verified], KNOWN)
    assert [p.candidate.id for p in plan] == [3]          # only the unreviewed copy goes


def test_of_several_copies_the_most_confident_is_kept():
    plan = plan_redundant([_c(1, "Registration Number", "REG-2024-88231", conf=0.6),
                           _c(2, "Reg. Number", "REG-2024-88231", conf=0.95)], KNOWN)
    assert [p.candidate.id for p in plan] == [1]
