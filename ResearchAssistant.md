# AI Research Assistant — Implementation Plan

## Where we are today

`services/chat.py` is a single-pass RAG chat: permission-aware retrieval, SSE
streaming, a refusal floor on insufficient grounding, a per-tenant token budget,
and a Q&A audit log. `Research.tsx` is a bare chat box.

The gaps that matter:

- Follow-ups embed the raw follow-up text, so "what about the second one?"
  retrieves noise.
- One retrieval pass, top-8 chunks — too small a slice for "across all our
  vendor contracts".
- The structured layer (field values, summaries, classifications) is unused.
- No scope: it always searches everything the caller can read.
- Citations are document-level, though chunks carry `page` and `bbox` and the
  viewer already highlights them.
- No history, scope, stop, regenerate or feedback in the UI.

## Settled decisions

1. **The tool contract and the research state are the architecture.** The agent
   topology is an implementation detail and is expected to change two or three
   times as we learn what it gets wrong.
2. **Few agents, many tools.** Orchestrator (one loop) → evidence workers (N
   parallel, isolated context) → synthesiser (one call) → validator (mostly
   code). Deterministic operations are tools, not agents.
3. **Scope means two different things.** For aggregate/comparative questions it
   is the *population* and must never silently widen; missing evidence surfaces
   as coverage. For reference questions it is a *focus hint* and escalation is
   correct. The orchestrator classifies which it is.
4. **Source policy is two dials, not four modes:** Reach (`scope` →
   `scope then corpus` → `corpus`) × General knowledge (`off` → `allowed,
   labelled per sentence`).
5. **Scope stores a rule and a resolved snapshot.** The snapshot is what gets
   searched (reproducibility); the rule powers a "4 new documents now match —
   add them?" nudge. Every use re-filters through `readable_documents_condition`.
6. **The agent may only propose scope changes; a human applies them.** This is
   the injection defence, not a UI convenience.
7. **Retrieval filters first, then computes exact similarity** for small scopes;
   HNSW with iterative scan for large ones. The arithmetic stays in Postgres.
8. **Tags are shared, flat, and human-set only.** Autocomplete and facet counts
   are permission-filtered.
9. **Research sessions extend `Conversation`** rather than forking a parallel
   chat model, so history, messages and citations are not built twice.

## Cross-cutting rules

- Every tool is read-only except scope *proposals*.
- Document text is untrusted input: delimited in prompts, and instructions found
  inside a document are never executed.
- Permissions are re-checked on every retrieval and on every reopen of history;
  citations to newly unreadable documents redact.
- Schema changes go in the tenant chain (`backend/alembic/versions/`) and must be
  applied with `python -m app.cli migrate-all-tenants`.
- Each phase lands with tests in the existing security-as-acceptance-criteria
  style, not only happy-path coverage.
- Budgets: the per-tenant daily token cap exists; per-session step, token and
  wall-clock caps must land before the orchestrator ships.

---

## Phase 1 — Tags, and the retrieval primitives

**Goal:** ship tags as a feature users benefit from immediately, and give
retrieval the three capabilities every later phase depends on.

**Build**

- `tags` + `document_tags`, case-insensitive unique per tenant, shared and flat.
  Relational rather than a JSONB array, because rename, merge, autocomplete and
  counts all want an indexed join.
- Tag API: create, rename, merge, delete, assign, unassign, list with counts.
- **Permission-filtered autocomplete and facets** — counts computed through
  `readable_documents_condition`, never off the raw tag table. A tag name leaks:
  offering "Project Falcon" to someone who can read none of its documents
  discloses that the project exists. Same class as the existing
  duplicate-non-disclosure policy.
- `list_documents` gains a `tags` filter (today it has only `q` and `status`).
- Bulk tagging from search results, using the multi-select control that Phase 3
  reuses for Research Sets — one component, two verbs.
- `document_ids` filter on `vector_search` / `keyword_search` / `hybrid_search`.
- Exact-vs-approximate branch: below a measured chunk-count threshold, force the
  exact path (`SET LOCAL enable_indexscan = off`) and skip the iterative-scan
  GUCs, which only affect the index path; above it, keep today's behaviour. This
  is a correctness fix as much as a performance one — HNSW with a filter cannot
  guarantee the true top-k, and "I searched all 12 documents" has to be true.
- `find_documents(filters)` — ANDed predicates (type, tags, field conditions,
  date, status) resolving to document IDs. Today `structured_search.query` is
  single-condition and returns field values, not documents.
- Catalog tools: `list_document_types`, `describe_document_type` (reusing
  `list_leaf_paths`), `field_values(field_key)` with counts.

The catalog is not an extra. Without distinct values the agent emits
`city = "Mumbai"` against data stored as `"Mumbai, MH"`, gets zero rows, and
reports "no matching documents" with full confidence. Silent-zero damages trust
more than a hallucination does.

**Schema:** `tags`, `document_tags`.

**Exit criteria**

- Tagging, filtering and faceting work end to end; autocomplete never reveals a
  tag whose documents the caller cannot read (test).
- Scoped vector search returns exact top-k, verified against a brute-force
  reference implementation (test).
- `find_documents` composes type + tag + field conditions in one query.

---

## Phase 2 — Trustworthy answers in the assistant that already exists

**Goal:** make today's single-pass chat trustworthy before adding any agent
machinery. Citation validation and coverage reporting are reused by every later
phase, and both are far easier to build and test against a single pass than
retrofitted into a multi-step agent.

**Build**

- Follow-up rewriting: resolve the question against history into a standalone
  query before retrieval.
- Deep-link citations: pass chunk `page` and `bbox` through to the client so a
  citation opens the viewer at the highlighted location. Both are already
  stored and the viewer already highlights.
- Citation validation: check each claim against the `[n]` it cites; flag
  unsupported sentences rather than presenting them as grounded.
- Coverage instead of a binary refusal: "found in 3 of 5 documents, nothing on
  X", plus documents searched and passages used.
- Conversation history UI: list, rename, delete, search. Stop, regenerate,
  edit-and-resend.
- Feedback (thumbs plus a reason) written into the existing `QAAuditLog`.
- Reopen re-check: redact citations to documents the caller can no longer read.

**Schema:** feedback columns on `qa_audit_logs`.

**Exit criteria**

- Every citation resolves to a chunk the caller can read (test).
- A revoked document's citations vanish from reopened history (test).
- A deliberately ungrounded answer is flagged by the validator (test).

---

## Phase 3 — Research sessions: scope and source policy

**Goal:** make the population of a question explicit and under the user's
control.

**Build**

- `Conversation` extended with source policy and research state; scope items in
  their own table.
- Scope as rule + snapshot; add/remove by document, type or tag; permission
  re-check on every use.
- Three ways to build scope, all producing the same structured object:
  1. **Research Set** from document search — multi-select, review, "Start
     Research".
  2. **In-chat natural language** — the agent proposes an editable ScopeRule and
     lists matching documents with checkboxes and links; the human applies it.
  3. **Scope panel** — direct editing. `/scope` is a shortcut to that panel, not
     a syntax to learn.
- Source-policy dials, visible and changeable mid-conversation.
- Population-vs-reference classification; population-bound questions never
  escalate, and escalated evidence is attributed by tier, never blended.
- Scope stays optional: start without one, add or change it at any point.

Tags turn out to be the best scope primitive available — human-curated,
deterministic, and immune to the silent-zero failure that field filters have.

**Schema:** `research_scope_items`, `research_sets`, policy and state columns on
`conversations`.

**Exit criteria**

- Scope can be built all three ways and modified mid-session.
- Under `scope only`, no citation ever originates outside the scope (test).
- An aggregate question reports coverage over the scope rather than a silently
  widened set (test).
- A scope change instructed by document text is never auto-applied (injection
  test).

---

## Phase 4 — The orchestrator

**Goal:** multi-step research with tools, visible to the user, inside a budget.
Deliberately stops short of fan-out.

**Build**

- Tool calling in `LLMProvider` — it has `complete`, `complete_json` and
  `astream` only — plus an offline stub so the suite keeps its offline path.
- One orchestrator loop owning the full tool set: `list_document_types`,
  `describe_document_type`, `field_values`, `find_documents`, `search_text`,
  `get_document`, `get_summary`, `get_fields`, `propose_scope_change`.
- A single `search_text(query, mode="hybrid")` rather than three near-identical
  search tools; overlapping tools make the model choose inconsistently.
- Research state recorded per step: queries generated, documents considered,
  evidence kept and rejected, steps taken.
- That state streamed over the existing SSE channel as the progress feed
  ("Searching 24 documents in scope… found evidence in 5…"). The observability
  feature and the debugging trace are the same object.
- Escalation under `scope then corpus`, with tier attribution in the answer.
- Hard caps on steps, tokens and wall clock, with a cost preview before
  expensive runs.
- **Interactive and streamed.** Background jobs are deferred to Phase 5, where
  fan-out makes runs long enough to need them.

**Schema:** `research_steps`, or a JSONB trace on the conversation.

**Exit criteria**

- A question single-pass RAG gets wrong — multi-hop, or needing a structured
  filter — is answered correctly with a visible trace.
- The loop terminates inside its caps on an adversarial question.
- The trace shows structured filtering used where it applies.

---

## Phase 5 — Scale: fan-out, comparison, corpus-wide

**Goal:** questions spanning tens or hundreds of documents.

**Build**

- Evidence workers: N parallel, one document or small batch each, isolated
  context, returning compact structured evidence or "nothing here".
- Synthesiser that sees only validated evidence, not the whole messy trace.
- Validator: deterministic where possible, one cheap LLM call for the rest.
- Map-reduce over per-document `Summary` rows for corpus-wide synthesis — those
  rows already exist and carry `highlights`, so no new infrastructure.
- Compare N documents across a type's fields; clause-level diff against a
  template or a prior version.
- Contradiction and gap detection; timeline from extracted dates.
- Background mode for runs exceeding the interactive budget: resumable, with
  notification on completion.
- Answers as tables with a source link per cell, and CSV export.

**Exit criteria**

- A 50-document scope answered inside budget and latency targets.
- Every cell of a comparison links to its source.
- A planted disagreement between two documents is detected.

---

## Phase 6 — Outputs

**Goal:** research leaves the chat.

**Build:** research notebook (findings saved with their citations), memo export
(DOCX / PDF / Markdown with footnoted citations), conversation sharing where the
recipient sees only what they are permitted to read.

**Exit criteria:** an exported memo's citations resolve; a shared conversation
redacts correctly for a less-privileged recipient (test).

---

## Deferred

- Approval workflow for publishing document types (deferred earlier, unchanged).
- AI-suggested tags seeded from the discovery pass's entities.
- MMR re-ranking, and session-level caching of a scope's vector matrix. Measure
  first. If cached, the permission filter must never come from the cache — with
  vectors in memory the readability re-check becomes a separate masking step,
  and a bug there is a disclosure bug rather than a wrong answer.
- Legal layer: a matter becomes another scope type, plus precedent search and
  matter kickoff. No core changes expected — which is the test of whether this
  architecture is extensible enough.

## Open questions

Both are recommendations baked into the phasing above, not settled calls:

- **Interactive vs background deep research.** Phase 4 is interactive with
  streamed steps and hard caps; background mode arrives in Phase 5 when fan-out
  makes runs long. The alternative is building the job/resume model in Phase 4.
- **Where Phase 4 stops.** It ends at the single orchestrator loop. The tool
  contract is what needs validating first, and the topology is cheap to change
  once the tools are proven.
