# AI-First Document Management System — MVP Feature List (v2, Reviewed)

**Target stack:** Python / FastAPI · Celery · PostgreSQL (RDBMS + pgvector) · React + TypeScript

> Revision notes: sections marked **[NEW]** were added; items marked *(added)* were inserted into existing sections. Rationale for each change is in the accompanying review summary. Everything else is unchanged from v1.

------------------------------------------------------------------------

## 1. User & Tenant Management

Manage organizations, users, authentication, and role-based access.

### Features

-   **Tenant Management** — Create and manage isolated organizations (tenants).
-   **User Management** — Invite, edit, deactivate, and remove users.
-   **Group Management** — Organize users into groups for easier permission assignment.
-   **Role Management** — Define roles such as Administrator, Reviewer, and Viewer.
-   **Authentication** — Secure login and identity verification (JWT-based sessions; design for OIDC/SSO later — don't hand-roll password auth deeper than necessary).
-   **Authorization** — Enforce role-based access across the application.
-   **User Profile** — Manage user profile and preferences.
-   *(added)* **Role/Permission interaction definition** — Roles gate *application capabilities* (Reviewer can access the review queue; Administrator can configure categories); per-document permissions (Section 4) gate *data access*. A Reviewer only sees review items for documents they can read. This distinction must be enforced in every API endpoint.

------------------------------------------------------------------------

## 2. Document Library

Central repository for storing and managing documents.

### Features

-   Upload Document
-   Download Document
-   View Document
-   Update Document (Versioning)
-   Delete Document (soft delete)
-   Restore Deleted Document *(define retention window, e.g., 30 days, then hard purge including chunks/embeddings)*
-   Version History
-   Search by Name
-   *(added)* **Upload validation** — max file size, format allow-list, MIME sniffing (don't trust extensions)
-   *(added)* **Duplicate detection** — content hash (SHA-256) on upload; warn or dedupe on exact match
-   *(added)* **Bulk upload** — multi-file upload with per-file status; this is how real corpora arrive

### Metadata

-   File Name
-   Description
-   File Type
-   File Size
-   *(added)* Content Hash
-   Uploaded By
-   Uploaded Date
-   Current Version
-   Processing Status

### [NEW] File Storage

-   Document binaries live in **object storage** (Azure Blob / S3 / MinIO for local dev) — **not** in Postgres. Postgres stores metadata + a storage key.
-   Versions are immutable blobs; a new version is a new object.
-   Downloads served via **short-lived pre-signed URLs** issued only after a permission check — the API must never proxy large file bytes through FastAPI workers.

------------------------------------------------------------------------

## 3. Category Management

Organize documents into predefined business categories.

### Features

-   Create Category
-   Edit Category
-   Enable/Disable Category
-   Delete Category *(define behavior: block deletion while documents are assigned, or require reassignment — pick one, recommend block)*
-   Category Description
-   *(added)* **Extraction schema binding** — each category references its extraction schema definition and version (see Section 9). Schema definitions are data (DB-stored JSON), not code, so Phase 3 schema evolution doesn't require redeployment.

**Initial Categories** — Contracts, Invoices

------------------------------------------------------------------------

## 4. Access Control

Protect documents using fine-grained permissions.

### Features

-   User Permissions
-   Group Permissions
-   Read Permission
-   Update Permission
-   Delete Permission
-   *(added)* **Manage/Share Permission** — the right to grant or revoke access on a document. Without this, only the answer to "who can share?" is "nobody" or "everybody."
-   *(added)* **Document Owner** — uploader is owner by default; owner implicitly holds Manage.
-   *(added)* **Category-level default permissions** — new documents inherit defaults from their category (e.g., Invoices readable by Finance group). Explicit per-document grants override.
-   Permission Management (grant/revoke UI + audit)
-   Tenant Isolation — enforce `tenant_id` on **every** query; strongly consider Postgres Row-Level Security as a backstop rather than relying on application-layer discipline alone.

### [NEW] Implementation note: permission-aware retrieval on this stack

Because the vector store **is Postgres**, do not denormalize ACLs into chunk metadata (that pattern is for external vector DBs and forces a reconciliation pipeline). Instead, resolve permissions **at query time with a join**: chunks → document → ACL tables, filtered by the caller's user/group memberships. Permission changes are then instantly effective everywhere — search, Q&A, structured search — with zero reindexing. This is the single biggest simplification the chosen stack buys; do not give it away.

------------------------------------------------------------------------

## 5. AI Processing Pipeline

Automatically process uploaded documents through an asynchronous pipeline.

### Pipeline Stages

1.  OCR & Text Extraction
2.  Document Understanding
3.  Category Classification
4.  AI Summary Generation
5.  Metadata Extraction *(depends on stage 3 output — extraction schema is chosen per predicted category; if a document is multi-label across Contracts and Invoices, run both schemas)*
6.  Chunking
7.  Embedding Generation

### Pipeline Features

-   Processing Status *(per-stage, not just per-document: `pending / running / succeeded / failed / skipped` for each stage)*
-   Retry Failed Stages *(resume from failed stage, not from the beginning — stages must checkpoint their outputs)*
-   Reprocessing *(full or from a chosen stage; must be **idempotent** — deterministic chunk IDs, upsert semantics, supersede-then-delete for embeddings)*
-   Error Tracking *(store exception class, message, traceback reference, and the model/prompt version in use at failure)*
-   Processing History
-   *(added)* **Dead-letter handling** — a document that exhausts retries lands in a visible "failed" state on the dashboard with a manual retry action; it must never silently disappear from the queue.
-   *(added)* **Low-confidence classification path** — define it: if classification confidence is below threshold, document is marked "Uncategorized," routed to the review queue, and **extraction (stage 5) is skipped** until a human assigns a category. Summarization, chunking, and embedding still proceed.
-   *(added)* **Per-document cost tracking** — record token usage / API cost per stage. You will need this the first week of a bulk migration.

### [NEW] Implementation note: Celery topology

-   Model the pipeline as a **Celery chain** of stage tasks; persist stage state in Postgres (a `pipeline_run` + `pipeline_stage` table), not in Celery result state.
-   **Avoid chords**; there is no fan-in requirement in this pipeline, and if one appears later, use an atomic DB counter instead.
-   **Separate queues by resource profile** — at minimum `q_ocr` (slow, memory-heavy), `q_llm` (rate-limit bound), `q_light` (chunking, bookkeeping) — with independent worker pools, so a 500-page scan doesn't starve classification of one-page invoices.
-   Set `acks_late` + idempotent tasks for crash safety; visibility-timeout tuning matters for long OCR tasks.
-   Broker: Redis is sufficient at MVP scale and doubles as the pub/sub layer for status streaming (below).

------------------------------------------------------------------------

## 6. Document Understanding

Convert documents into structured content.

### Features

-   OCR for scanned documents
-   Native text extraction
-   Table extraction
-   Layout analysis
-   Reading order detection
-   Section detection
-   Image extraction
-   *(added)* **Positional data capture** — OCR/parsing must retain page numbers and, for PDFs, bounding boxes per text block. Citation highlighting in the viewer (Section 15) is impossible to retrofit if coordinates were discarded at ingestion.

Supported Formats: PDF, DOCX, PPTX, XLSX, CSV, TXT, Images

*(added)* **Format note:** "page" is only well-defined for PDF and images. For DOCX/PPTX/XLSX, provenance anchors to section/slide/sheet+cell-range respectively — define the anchor type per format now so the provenance data model (Section 9) handles all of them.

------------------------------------------------------------------------

## 7. AI Category Classification

Automatically predict document categories.

### Features

-   Multi-label classification
-   Confidence score *(per label, not per document)*
-   Manual override
-   Category review
-   *(added)* Corrections captured as structured (input, prediction, correction) records — this is the Phase 3 learning-loop feedstock; capture it from day one even though nothing consumes it yet.

------------------------------------------------------------------------

## 8. AI Summarization

Generate concise document summaries.

### Features

-   Executive Summary
-   Key Highlights
-   Store AI summary *(labeled as AI-generated in the UI)*
-   Model version tracking
-   Prompt version tracking

------------------------------------------------------------------------

## 9. Metadata Extraction

Extract structured business information.

### Contract Fields

Effective Date, Expiration Date, Parties, Contract Value, Governing Law, Payment Terms, Renewal Terms, Termination Clause

### Invoice Fields

Invoice Number, Vendor, Invoice Date, Due Date, Currency, Total Amount, Tax Amount

### Each extracted value stores

-   Value — *(added)* stored **twice**: the raw extracted text, and a **normalized typed value** (`date`, `numeric`, `text`, `currency+amount`). Structured search (Section 14) queries the typed value; the raw text is what the human sees and verifies. Normalization (date parsing, currency codes, thousand separators) is part of the extraction stage, and normalization failures lower confidence.
-   Confidence Score
-   Source Page *(or format-appropriate anchor — see Section 6)*
-   Source Text
-   *(added)* Source bounding box / character offsets where available
-   Review Status (`auto_accepted / pending_review / verified / corrected / rejected`)
-   *(added)* Model + prompt version that produced it
-   *(added)* Schema version (which field definition it was extracted under)

------------------------------------------------------------------------

## 10. Review Queue

Human validation for low-confidence AI results.

### Features

-   Pending Review Queue *(covers both extractions and low-confidence classifications)*
-   Accept Extraction
-   Modify Extraction
-   Reject Extraction
-   Reviewer Audit Trail
-   Capture Corrections
-   *(added)* **Claim/assignment** — a reviewer claims an item (optimistic lock) so two reviewers don't process the same item; unclaimed after timeout returns to queue.
-   *(added)* **Filters & sorting** — by category, field, confidence, age.
-   *(added)* **Side-by-side review UI** — extracted value + source passage highlighted in the document viewer in one screen. Review throughput lives or dies on this screen; budget UI effort accordingly.
-   *(added)* **Queue aging visibility** — oldest-item age on the dashboard.

------------------------------------------------------------------------

## 11. Chunking & Embeddings

Prepare documents for semantic search.

### Features

-   Semantic Chunking
-   Context Preservation
-   Chunk Metadata *(page/anchor, section title, category, document version)*
-   Embedding Generation
-   Version-aware Indexing *(new version's chunks are written, then prior version's chunks deleted, in one transaction — retrieval never sees a mixed state)*
-   Permission-aware Indexing *(per Section 4: permissions resolved by join at query time; chunks store `document_id` only, no ACL denormalization)*
-   *(added)* **Deterministic chunk IDs** — content + position derived, so reprocessing is idempotent.
-   *(added)* **Embedding model pinning** — record embedding model + version per chunk; a model change requires full re-embed, so it must be detectable and migratable.

### [NEW] Implementation note: pgvector

-   Use **HNSW** index (not IVFFlat) for the MVP corpus size.
-   Known sharp edge: HNSW scans apply `WHERE` filters *during* graph traversal and can return **fewer than `LIMIT k` rows** when the filter (tenant + ACL join) is selective. Require **pgvector ≥ 0.8** and enable **iterative index scans** (`hnsw.iterative_scan`), and integration-test retrieval specifically as a low-privilege user with access to few documents — that's where this bug bites.
-   Keep embeddings in a dedicated table; don't widen the chunks table with a vector column plus large text columns (TOAST churn on updates).

------------------------------------------------------------------------

## 12. Semantic Search

Search documents using natural language.

### Features

-   Vector Search
-   Keyword Search *(Postgres full-text search — `tsvector` + GIN — no extra infra needed)*
-   Hybrid Search *(Reciprocal Rank Fusion over the two result lists is simple and good enough for MVP)*
-   Permission-aware Retrieval
-   Search Ranking

------------------------------------------------------------------------

## 13. AI Research Assistant

Chat with enterprise knowledge.

### Features

-   Natural Language Questions
-   Multi-document Answers
-   Grounded Responses
-   Citations *(each answer claim links to chunk → document + page/anchor; clicking opens the viewer at the cited location)*
-   Follow-up Questions
-   Permission-aware Answers
-   *(added)* **Refusal on insufficient grounding** — if retrieval yields nothing adequate from the caller's authorized corpus, the assistant says so explicitly. This behavior is a feature with acceptance criteria, not a hope.
-   *(added)* **Conversation persistence** — store conversations per user (history, cited sources); follow-up questions need it anyway.
-   *(added)* **Streaming responses** — SSE from FastAPI (`StreamingResponse` + `EventSource` client). Same pattern as pipeline status streaming; share the infrastructure.
-   *(added)* **Per-tenant rate limiting & token budget** — the chat endpoint is the cost hot spot; meter it from day one.
-   *(added)* **Q&A audit log** — question, retrieved chunk IDs, answer, model/prompt version. Needed for debugging groundedness complaints and for security review.

------------------------------------------------------------------------

## 14. Structured Search

Query extracted metadata.

### Examples

-   Contracts expiring in next six months
-   Invoices above a value
-   Contracts by governing law
-   Contracts by vendor

*(added)* Queries run against **normalized typed values** (Section 9). Results display the coverage caveat where relevant: values pending review are flagged or excluded per a user-visible toggle ("verified only").

------------------------------------------------------------------------

## 15. Document Viewer

Rich document viewing experience.

### Features

-   PDF Viewer *(pdf.js; text layer required for highlighting)*
-   Page Navigation
-   Citation Highlighting *(consumes bounding boxes / offsets from Section 6; for non-PDF formats MVP fallback = show source text snippet + anchor rather than in-document highlight)*
-   Metadata Panel *(extracted fields with confidence + review status; click a field → jump to its source location)*
-   Summary Panel
-   Version Selection
-   Processing Timeline

------------------------------------------------------------------------

## 16. Dashboard

Provide operational insights.

### Widgets

-   Total Documents
-   Documents by Category
-   Processing Status *(including failed/dead-lettered count)*
-   Pending Reviews *(count + oldest-item age)*
-   Recent Uploads
-   *(added)* Extraction auto-accept rate (7/30-day) — the one number that says whether the AI is earning trust
-   *(added)* AI processing cost (period-to-date)

------------------------------------------------------------------------

## 17. Background Job Management

Manage asynchronous processing.

### Features

-   Job Queue
-   Retry Failed Jobs
-   Resume Jobs *(from failed stage)*
-   Cancel Jobs
-   Reprocess Documents
-   Job Monitoring *(Flower or equivalent for Celery internals; the admin UI reads the `pipeline_run` tables — don't build a Celery UI from scratch)*
-   *(added)* **Live status updates to UI** — Redis pub/sub → SSE, so upload → processing → done updates without polling.

------------------------------------------------------------------------

## 18. Administration

Application-wide configuration.

### Features

-   Category Configuration
-   AI Model Configuration *(provider, model name, per-stage overrides)*
-   Prompt Version Management *(prompts stored as versioned data, not hardcoded strings; every AI output references the prompt version used)*
-   Confidence Thresholds *(per category **and per field** — dates and totals will hit auto-accept long before termination clauses do)*
-   Processing Configuration *(chunk size/overlap, file size limits, retry policy)*

------------------------------------------------------------------------

## 19. Audit Logs

Track important system events.

### Features

-   Upload History
-   Delete History
-   Permission Changes
-   Metadata Changes
-   Review Activity
-   Login History
-   *(added)* Download/access events *(who opened/downloaded which document — expected in any security review)*
-   *(added)* Admin configuration changes *(thresholds, prompts, model settings)*

------------------------------------------------------------------------

## 20. [NEW] Notifications

Minimal but necessary for the review workflow to function.

-   In-app notifications: processing complete / processing failed (to uploader), items pending review (to reviewers)
-   Email: optional at MVP; design the notification event model so email is an added channel, not a rewrite

------------------------------------------------------------------------

## 21. [NEW] Non-Functional Requirements (MVP acceptance bar)

-   **Security tests as acceptance criteria:** automated tests proving (a) cross-tenant retrieval is impossible, (b) a user without Read on a document can never receive its chunks via search, Q&A, or structured search, (c) permission revocation is effective on the next query.
-   **Extraction quality gate:** per-field precision/recall measured on a human-verified holdout set before enabling auto-accept for that field.
-   **Idempotency:** reprocessing any document twice yields identical chunk/embedding/metadata state (no duplicates).
-   **Groundedness:** answer citations resolve to real chunks the caller is permitted to read; sampled manual audit of N answers per release.
-   **Observability:** structured logging with correlation IDs from API request → Celery task → model call; per-stage latency and cost metrics.

------------------------------------------------------------------------

# Future Features (Post-MVP)

## Phase 2

-   Additional document categories
-   Knowledge extraction
-   Clause extraction
-   Cross-document comparison
-   Analytics dashboards *(with the stricter aggregate-permission model and small-sample suppression)*
-   AI category suggestions
-   Medical document support *(gated on category-level governance controls)*
-   Resume support *(gated on PII handling/retention controls)*
-   External identity providers (OIDC/SSO), SCIM provisioning
-   Webhooks / API for external integrations

## Phase 3

-   AI schema evolution *(with managed backfill jobs + cost estimation)*
-   AI taxonomy evolution
-   Knowledge graph
-   Continuous learning *(consumes the correction records captured since MVP)*
-   Automatic field discovery
-   Automation graduation
-   Emerging document detection
-   Dedicated vector store migration *(only if pgvector demonstrably becomes the bottleneck — measure first)*

------------------------------------------------------------------------

# Appendix — Stack Decisions Summary

| Concern | Decision | Note |
|---|---|---|
| API | FastAPI | Async endpoints; SSE for streaming status + chat |
| Background jobs | Celery + Redis broker | Chains per pipeline; per-resource-profile queues; state in Postgres, not Celery results |
| RDBMS | PostgreSQL | RLS as tenant-isolation backstop; FTS for keyword search |
| Vector store | pgvector (≥ 0.8) | HNSW + iterative scans; ACLs via join, never denormalized |
| Blob storage | Azure Blob / S3 / MinIO | Pre-signed URLs; Postgres stores keys only |
| Frontend | React + TypeScript | Vite, TanStack Query for server state, pdf.js viewer |
| Realtime | Redis pub/sub → SSE | One mechanism serves pipeline status and chat streaming |
