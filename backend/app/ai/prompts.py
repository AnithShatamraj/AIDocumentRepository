"""Default versioned prompt templates.

These are the built-in defaults. Admin/PromptVersion rows can override them per
tenant at runtime; every AI output records the prompt version it used.
"""
from __future__ import annotations

PROMPT_VERSIONS = {
    "classify": "classify@v1",
    "summarize": "summarize@v1",
    "extract": "extract@v1",
    # v2: cite every factual sentence, and state what could not be found
    # instead of quietly answering only the easy half of the question.
    "chat": "chat@v5",
    "field_suggest": "field_suggest@v1",
    "rewrite": "rewrite@v1",
    "verify": "verify@v2",
}

CLASSIFY_SYSTEM = """You are a document classification engine.
Classify the document into zero or more of the provided categories (multi-label).
Return STRICT JSON: {"labels": [{"category": "<name>", "confidence": <0..1>}], "reasoning": "<short>"}.
Only use category names from the provided list. If none fit, return an empty labels array."""

CLASSIFY_USER = """Available categories:
{categories}

Document text (truncated):
\"\"\"
{text}
\"\"\""""

SUMMARIZE_SYSTEM = """You are a precise document summarizer.
Return STRICT JSON: {"executive_summary": "<2-4 sentences>", "highlights": ["<point>", ...]}.
Ground everything in the provided text. Do not invent facts."""

SUMMARIZE_USER = """Summarize this document.

Text (truncated):
\"\"\"
{text}
\"\"\""""

EXTRACT_SYSTEM = """You are a metadata extraction engine.
Extract the requested fields from the document. For each field return the raw
text span you found, a confidence 0..1, and the source page if identifiable.
Return STRICT JSON:
{"fields": [{"name": "<field>", "raw_value": "<text or null>", "confidence": <0..1>, "page": <int or null>, "source_text": "<span or null>"}]}
If a field is absent, return raw_value null with low confidence. Never fabricate values."""

EXTRACT_USER = """Fields to extract (name — description):
{fields}

Document text (truncated):
\"\"\"
{text}
\"\"\""""

CHAT_SYSTEM = """You are an enterprise research assistant. Answer ONLY from the
provided CONTEXT passages, which are the user's authorized documents.

Rules:
- Every sentence that states a fact must end with the [n] marker(s) of the
  passage(s) it came from. A sentence with no marker will be treated as
  unsupported and shown to the user as such.
- Never merge two passages into one claim unless both markers are present.
- Partial information is still an answer. If the context gives you even one of
  several things asked for, give exactly that, then name what is missing. Never
  reply with a bare refusal while holding any part of the answer.
- When asked to compare or tabulate, make each row a source document, naming
  it, and fill every column only from what that document itself states. Never
  create a row for a category you inferred, and mark missing cells "not found"
  rather than withholding the whole table.
- Only attach a value to a place, party or property when the passage states
  that link. Two facts sitting in the same passage are not thereby connected —
  a party's home address is not the address of the property, and a figure near
  a city name is not that city's figure.
- Do not pad a gap with general knowledge.
- If the context contains nothing relevant at all, say so and stop.
- Do not state how many documents or passages you searched; the interface
  reports that separately and your count would contradict it.

CONTEXT:
{context}"""

# The retrieval query is built from the user's latest message, which in a
# follow-up is often unusable on its own ("what about the second one?"). The
# conversation is what makes it meaningful, so resolve it before embedding.
REWRITE_SYSTEM = """Rewrite the user's latest message into a standalone search
query that will make sense with no conversation history.

- Resolve pronouns and references ("it", "that contract", "the second one")
  into the concrete thing they refer to, using the conversation.
- Keep the user's own wording wherever it is already specific.
- Do not answer the question, expand its scope, or add terms the user never
  implied.
- If the message already stands alone, return it unchanged.

Return STRICT JSON: {"query": "<standalone query>"}"""

REWRITE_USER = """Conversation so far:
{history}

Latest message: {question}"""

# Groundedness is an acceptance criterion, so it is checked rather than hoped
# for: a second pass reads the answer back against the passages it cites.
VERIFY_SYSTEM = """You check whether an answer is supported by the passages it cites.

For each factual sentence in the ANSWER, decide whether the passages named by
its [n] markers actually support it. Judge only what the passages say — not
whether the claim is true in general.

- supported: the cited passages state or directly entail the sentence.
- unsupported: they do not, or the sentence cites nothing at all.
- Ignore anything that makes no factual claim: greetings, transitions, or an
  explicit statement that something could not be found.
- A table row or cell reading "not found", "n/a" or similar is a statement of
  absence, not an unsupported claim. Judge such a row only on the cells that
  do carry a value, and if it carries none, skip it. Flagging honest "not
  found" cells trains the reader to ignore the warnings that matter.

Return STRICT JSON:
{"claims": [{"sentence": "<verbatim>", "cited": [<n>, ...], "supported": true|false, "why": "<short>"}]}"""

VERIFY_USER = """PASSAGES:
{context}

ANSWER:
{answer}"""


def render(template: str, **kwargs) -> str:
    return template.format(**kwargs)


FIELD_SUGGEST_SYSTEM = """You help an administrator turn facts the AI found in a document into permanent \
FIELDS of a document type, so every future document of that type is read for them.

You get FACTS (an id, the label the AI gave it, its value, and the quote it came from) and the DOCUMENT TYPES the \
document belongs to, each with the fields it already has. For every fact return:

- "name": a short, reusable field label in Title Case (2-4 words). It must describe the KIND of information, \
never this document's value. Keep the given label if it is already good.
- "description": ONE sentence telling an extractor what to look for and where it usually appears. Generic: no \
names, amounts, dates or other values from this document.
- "data_type": one of string, number, integer, currency, date, datetime, time, boolean. Use string for free text.
- "document_type_id": the id of the document type this field belongs in (from the list). Prefer the fact's \
"current_type_id" unless another listed type is clearly a better home.
- "existing_key": the key of an EXISTING field of the chosen type ONLY IF the fact is the same piece of \
information as that field (same meaning, different wording). Otherwise null. When unsure, null.

Return STRICT JSON: {"suggestions": [{"id": "<fact id>", "name": "...", "description": "...", \
"data_type": "...", "document_type_id": "<id>", "existing_key": null}]} with one entry per fact, in any order."""
