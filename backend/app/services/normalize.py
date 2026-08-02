"""Normalize raw extracted text into typed values for structured search.

Normalization failures lower confidence (the caller applies the penalty).
"""
from __future__ import annotations

import datetime as dt
import re

from app.models.constants import VT_CURRENCY, VT_DATE, VT_NUMERIC, VT_TEXT

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}

_DATE_FORMATS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d-%m-%Y",
    "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y",
]
# Month-precision dates are everywhere in real documents ("March 2021" employment
# ranges, "Q1 2024" contract months). Parsed to the first of the month rather than
# left unnormalized, which would otherwise dock confidence and force a review.
_MONTH_FORMATS = ["%B %Y", "%b %Y", "%Y-%m", "%m/%Y", "%m-%Y"]


class Normalized:
    def __init__(self, value_type: str, text=None, number=None, date=None, currency=None, ok=True):
        self.value_type = value_type
        self.value_text = text
        self.value_number = number
        self.value_date = date
        self.value_currency = currency
        self.ok = ok


def _parse_date(raw: str) -> dt.date | None:
    raw = raw.strip().rstrip(".")
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    for fmt in _MONTH_FORMATS:  # month precision -> first of that month
        try:
            return dt.datetime.strptime(raw, fmt).date().replace(day=1)
        except ValueError:
            continue
    return None


def _parse_number(raw: str) -> tuple[float | None, str | None]:
    currency = None
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in raw:
            currency = code
            break
    m = re.search(r"-?[\d,]*\.?\d+", raw.replace(",", ""))
    if not m:
        return None, currency
    try:
        return float(m.group(0)), currency
    except ValueError:
        return None, currency


def normalize(raw_value: str | None, declared_type: str) -> Normalized:
    if raw_value is None or not str(raw_value).strip():
        return Normalized(declared_type or VT_TEXT, ok=False)
    raw = str(raw_value).strip()

    if declared_type == VT_DATE:
        d = _parse_date(raw)
        return Normalized(VT_DATE, text=raw, date=d, ok=d is not None)

    if declared_type == VT_NUMERIC:
        n, _ = _parse_number(raw)
        return Normalized(VT_NUMERIC, text=raw, number=n, ok=n is not None)

    if declared_type == VT_CURRENCY:
        n, cur = _parse_number(raw)
        return Normalized(VT_CURRENCY, text=raw, number=n, currency=cur or "USD", ok=n is not None)

    return Normalized(VT_TEXT, text=raw, ok=True)
