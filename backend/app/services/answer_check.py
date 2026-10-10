"""Read an answer back against the passages it cites.

Groundedness is an acceptance criterion in the spec, not an aspiration, so it
gets checked rather than trusted. Two layers:

1. A deterministic pass that finds factual-looking sentences carrying no `[n]`
   marker at all. This needs no model, never fails, and catches the most
   common failure — a fluent sentence with nothing behind it.
2. A model pass that reads each cited sentence against the passages it names
   and says whether they actually support it.

The distinction that matters is `checked`. When the model pass cannot run --
offline, budget exhausted, provider error -- the result says so rather than
returning an empty list of problems, because "nothing was found wrong" and
"nothing was looked at" must never render the same way to a user deciding
whether to trust an answer.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass

from app.ai import prompts
from app.ai.registry import get_llm, is_offline
from app.ai.types import Message as LLMMessage
from app.core.config import settings

CITE_RE = re.compile(r"\[(\d{1,2})\]")
# Split on sentence-ending punctuation, allowing citation markers and closing
# quotes to trail the terminator before the following space.
SENTENCE_RE = re.compile(r"(?<=[.!?])[\"')\]]*\s+")

# A comparison answer is usually a table, and a table row carries a claim while
# containing no sentence terminator. Splitting on punctuation alone let whole
# tabular answers past the checker unexamined.
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|[\s|:\-]+\|\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")

# "not found", "n/a", "—": a cell saying the value is absent. Flagging these as
# unsupported trains the reader to ignore the warnings that matter, so they are
# recorded but never raised.
_ABSENT_CELL = re.compile(r"^(?:not\s*found|not\s*stated|n/?a|none|nil|-{1,3}|—)?$", re.I)
_ABSENCE_TEXT = re.compile(r"\b(?:not\s*found|not\s*stated|no information|n/a)\b", re.I)


def _row_cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def row_asserts_value(row: str) -> bool:
    """Does a table row make a positive claim?

    Only if some cell *after the first* carries a real value. The first cell is
    the row label — often a filename, whose digits would otherwise make an
    all-"not found" row look like data.
    """
    return any(not _ABSENT_CELL.match(c) for c in _row_cells(row)[1:])

# Sentences that assert nothing, so an absent citation isn't a problem. Kept
# deliberately small: over-matching here is how an unsupported claim slips
# through as "just a transition".
_NON_CLAIM = re.compile(
    r"^\s*(?:"
    r"i (?:could not|couldn't|cannot|can't|don't|do not)\b"
    r"|(?:there is|there's) (?:no|nothing)\b"
    r"|no (?:information|mention|reference|document)\b"
    r"|(?:here|the following) (?:is|are)\b"
    r"|in summary\b|overall\b|however\b|additionally\b"
    r")",
    re.IGNORECASE,
)


@dataclass
class Claim:
    sentence: str
    cited: list[int]
    supported: bool
    why: str


def split_sentences(text: str) -> list[str]:
    """Break an answer into checkable units.

    Line-aware, because a markdown table row or a bullet is a claim with no
    sentence-ending punctuation: a purely punctuation-based split walked
    straight past tabular answers, which is precisely the shape a "compare
    these" question produces.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    units: list[str] = []
    i = 0
    while i < len(lines):
        s = lines[i]
        if not s or TABLE_SEP_RE.match(s):  # blank, or the |---|---| rule row
            i += 1
            continue
        # A header row is the one directly above the rule. It labels columns
        # rather than asserting anything, so drop it with the rule itself —
        # structural, instead of guessing from its contents.
        if TABLE_ROW_RE.match(s) and i + 1 < len(lines) and TABLE_SEP_RE.match(lines[i + 1]):
            i += 2
            continue
        if TABLE_ROW_RE.match(s) or BULLET_RE.match(s):
            units.append(s)
            i += 1
            continue
        units.extend(x.strip() for x in SENTENCE_RE.split(s) if x.strip())
        i += 1
    return units


def cited_indices(sentence: str) -> list[int]:
    """The [n] markers in a sentence, de-duplicated, in order of appearance."""
    out: list[int] = []
    for m in CITE_RE.finditer(sentence):
        n = int(m.group(1))
        if n not in out:
            out.append(n)
    return out


def is_claim(sentence: str) -> bool:
    """Does this sentence assert something that ought to be cited?"""
    stripped = sentence.strip()
    if TABLE_ROW_RE.match(stripped):
        # A row carrying a value is a claim; a header row, or one whose cells
        # all say "not found", is not.
        return row_asserts_value(stripped)
    if len(stripped) < 25:  # "Yes.", "Two of them." -- too short to stand alone
        return False
    return not _NON_CLAIM.match(stripped)


def uncited_claims(answer: str) -> list[Claim]:
    """Factual-looking sentences with no citation marker. No model required."""
    return [
        Claim(sentence=s, cited=[], supported=False, why="No source cited.")
        for s in split_sentences(answer)
        if is_claim(s) and not cited_indices(s)
    ]


def _coerce(raw: dict, valid_max: int) -> list[Claim]:
    out: list[Claim] = []
    for item in raw.get("claims") or []:
        if not isinstance(item, dict):
            continue
        sentence = str(item.get("sentence") or "").strip()
        if not sentence:
            continue
        cited = [n for n in (item.get("cited") or [])
                 if isinstance(n, int) and 1 <= n <= valid_max]
        out.append(Claim(
            sentence=sentence,
            cited=cited,
            # Anything not explicitly true is treated as unsupported: a model
            # that omits or garbles the flag should not earn a pass.
            supported=item.get("supported") is True,
            why=str(item.get("why") or "")[:280],
        ))
    return out


async def check(answer: str, context: str, passage_count: int) -> dict:
    """Verify an answer against its passages.

    Returns `{checked, claims, unsupported, source}` where `source` is
    "model" or "heuristic". `checked` is False when the model pass could not
    run, in which case only the deterministic findings are present and the UI
    must not claim the answer was verified.
    """
    heuristic = uncited_claims(answer)
    blank = {
        "checked": False,
        "source": "heuristic",
        "claims": [asdict(c) for c in heuristic],
        "unsupported": [asdict(c) for c in heuristic],
    }
    if not answer.strip() or is_offline():
        return blank

    messages = [
        LLMMessage("system", prompts.VERIFY_SYSTEM),
        LLMMessage("user", prompts.render(prompts.VERIFY_USER, context=context, answer=answer)),
    ]
    try:
        # complete_json is synchronous; off the event loop so a slow provider
        # doesn't stall every other request on this worker.
        raw, _usage = await asyncio.to_thread(
            get_llm().complete_json, messages, model=settings.llm_chat_model)
    except Exception:  # noqa: BLE001
        # A failed check reports as unchecked, never as "found nothing wrong".
        return blank

    claims = _coerce(raw, passage_count)
    if not claims:
        return blank
    # A claim that reports an absence ("rent: not found") is honest reporting,
    # not an unsupported assertion — the model flags these anyway, and a
    # warning panel full of them teaches the reader to skip the real ones. Kept
    # in `claims` for the record, dropped from what gets raised.
    unsupported = [c for c in claims
                   if not c.supported and not _ABSENCE_TEXT.search(c.sentence)]
    return {
        "checked": True,
        "source": "model",
        "claims": [asdict(c) for c in claims],
        "unsupported": [asdict(c) for c in unsupported],
    }
