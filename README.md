# AI Document Repository

An AI-first document management system: multi-tenant, permission-aware, with an
asynchronous AI ingestion pipeline (agentic, built on **deepagents**),
**pgvector** semantic search, grounded RAG Q&A with citations, and a
human-in-the-loop review queue.

Built to the spec in [`AIDocumentManagentFeatures.md`](AIDocumentManagentFeatures.md).

> **Status:** working core, end-to-end. The hard, easy-to-get-wrong parts are
> implemented properly (permission-by-join retrieval, resumable checkpointed
> pipeline, version-aware indexing, groundedness/refusal). Some breadth items are
> honest extension points — see [Implemented vs. extension points](#implemented-vs-extension-points).

---

## Architecture

| Concern | Choice |
|---|---|
| API | FastAPI (async), SSE for chat + pipeline status |
| Background jobs | Celery + Redis broker; **chain** per pipeline; queues by resource profile (`q_ocr`/`q_llm`/`q_light`); state in Postgres |
| RDBMS + vectors | PostgreSQL + **pgvector ≥ 0.8** (HNSW + iterative scans) |
| Blob storage | Cloud-agnostic: MinIO (dev) / S3 / Azure Blob / GCS; pre-signed URLs; Postgres stores keys only |
| PDF understanding | **IBM Docling** (`docling-serve`) as an independently scalable service — layout, bounding boxes, OCR for scanned/vector PDFs; pdfplumber fallback |
| Agentic ingestion | **deepagents** (LangChain/LangGraph) for classification, metadata, taxonomy — with direct-LLM and offline-heuristic fallbacks |
| AI providers | Config-driven, multi-provider. **OpenAI** default; deterministic **stub** provider so it runs with zero keys |
| Frontend | React + TypeScript (Vite), TanStack Query, **pdf.js viewer** with bbox highlight overlays |
| Realtime | Redis pub/sub → SSE (one mechanism for pipeline status and chat streaming) |

### PDF pipeline (Docling)

PDFs are parsed by the `docling` compose service (official `docling-serve-cpu`
image — prebuilt, models included, nothing to build). The worker's
`text_extraction` stage submits the file async and polls; results are normalized
to per-page **blocks with top-left-origin bounding boxes**, which power:

- extracted-field **source highlighting** in the viewer (`Extraction.source_bbox`
  via a fuzzy locator), and per-chunk regions (`Chunk.bbox`) for future citation
  highlighting;
- **OCR** for scanned PDFs (`DOCLING_OCR=auto` escalates to full-page OCR when a
  textless PDF yields nothing from the standard pass);
- the `GET /documents/{id}/layout` endpoint for viewer overlays.

Uploads of truncated PDFs (missing `%%EOF`) are rejected at validation, and a
PDF that still produces no text **fails the pipeline visibly** (dead-letter)
instead of silently proceeding with empty input. Scale `docling` independently
in prod (`k8s/docling.yaml`); switch to a GPU image variant for heavy OCR volume.

```
Browser (React/Vite)
      │  REST + SSE
      ▼
FastAPI ──────────► Postgres (+ pgvector)  ◄────── Celery workers
  │  presigned URL        ▲   ▲                        │
  ▼                       │   │ pipeline state         │ Redis (broker + pub/sub)
Object storage ───────────┘   └────────────────────────┘
(MinIO/S3/Azure/GCS)
```

### The AI pipeline (Celery chain)

`text_extraction → document_understanding → classification → summarization →
metadata_extraction → chunking → embedding`

- Per-stage state in `pipeline_runs` / `pipeline_stages` (not the Celery result backend).
- **Resumable** from the failed stage; **idempotent** (deterministic chunk IDs, upserts).
- **Low-confidence classification** → document marked *Uncategorized*, routed to the
  review queue, extraction **skipped** until a human assigns a category.
- **Version-aware indexing**: new version's chunks written, prior version's deleted in
  one transaction — retrieval never sees a mixed state.
- Every AI output records **model + prompt version**; per-stage **cost** tracked.
- Dead-letter: exhausted retries land the document in a visible **failed** state + notify uploader.

---

## Prerequisites

- **Docker Desktop** (for Postgres, Redis, MinIO — and, on Windows, the backend too).
- **Node 20+** (to run the React dev server natively).
- Python is **not** required on the host — the backend runs in a container.

---

## Quickstart

```bash
cp .env.example .env          # defaults work out of the box (offline stub AI)

# 1) Bring up infra (Postgres + Redis + MinIO + bucket)
docker compose up -d postgres redis minio minio-setup

# 2) Bring up the backend + Celery worker (initializes DB + seeds on first run)
docker compose --profile apps up --build api worker

# 3) Frontend — run natively for the best dev loop:
cd frontend && npm install && npm run dev
#   ...or in a container:  docker compose --profile apps up frontend
```

Then open **http://localhost:5173** and sign in with the seeded admin:

```
admin@acme.com  /  admin12345
```

- API docs: http://localhost:8000/docs
- MinIO console: http://localhost:9001 (minioadmin / minioadmin)

> With no `OPENAI_API_KEY`, the app runs on the **deterministic offline stub**:
> keyword classification, regex extraction, hashing embeddings. Everything works
> end-to-end (upload → pipeline → search → Q&A → review) with zero external calls.

### Turning on real AI (OpenAI + deepagents)

Set in `.env` and restart `api` + `worker`:

```
AI_DEFAULT_PROVIDER=openai
EMBEDDING_PROVIDER=openai
OPENAI_API_KEY=sk-...
INGESTION_USE_DEEPAGENTS=true
```

Classification and metadata extraction then run through a **deepagents** agent
(falling back to a direct structured-LLM call if the agent errors).

---

## Configuration

All settings live in `.env` (see `.env.example` for the annotated list). Highlights:

- `STORAGE_PROVIDER` = `minio|s3|azure|gcs` — Azure Blob is wired for the preferred cloud.
- `LLM_*_MODEL` — per-stage model overrides.
- `EMBEDDING_DIM` — must match the model (1536 for `text-embedding-3-small`). Changing
  it requires a re-embed and a matching pgvector column dimension.
- `CLASSIFY_CONFIDENCE_THRESHOLD`, `EXTRACT_AUTOACCEPT_THRESHOLD` — routing to review.
- `CHAT_MAX_TOKENS_PER_DAY` — per-tenant chat budget.

---

## Security invariants (enforced, not aspirational)

- **Tenant isolation** on every query via `tenant_id`.
- **Permission-aware retrieval by JOIN** — ACLs resolved live (chunks → document →
  ACL tables) against the caller's user + group memberships. No ACL denormalization,
  so revocation is effective on the **next** query with zero reindexing.
- A user without Read on a document can never receive its chunks via search, Q&A, or
  structured search. See `backend/tests/test_permissions.py`.
- **Groundedness**: the assistant **refuses** when retrieval yields nothing adequate
  from the caller's authorized corpus; every answer is audit-logged.

---

## Repo layout

```
backend/
  app/
    core/        config, db (async+sync), redis, security, logging
    models/      SQLAlchemy models (tenant, docs, ACL, pipeline, content, review, chat, ops)
    storage/     cloud-agnostic blob backends + factory
    ai/          provider registry (openai/stub), prompts, pricing
      agents/    deepagents / direct / heuristic ingestion strategies
    services/    parsing, chunking, normalize, permissions, search, chat, structured_search, audit
    worker/      celery app + pipeline stage tasks
    api/         routers + deps  ·  schemas.py  ·  main.py  ·  cli.py
frontend/        React + TS (Vite) SPA
k8s/             cloud-agnostic Kubernetes manifests (AKS-oriented)
```

---

## Production / Kubernetes (AKS-preferred)

The app is cloud-agnostic; Azure is the default target.

- **Database:** Azure Database for PostgreSQL Flexible Server with the `vector`
  extension (or RDS/Cloud SQL). Run `python -m app.cli init-db` as a Job.
- **Redis:** Azure Cache for Redis (or ElastiCache / Memorystore).
- **Blob:** Azure Blob (`STORAGE_PROVIDER=azure`) via workload identity, or S3/GCS.
- **Compute:** FastAPI + Celery + the React build on **AKS**. See `k8s/` for
  Deployments, Services, HPA, and an Ingress. Secrets come from a Secret / Key Vault.

For schema migrations in production, adopt **Alembic** (`alembic init`, autogenerate
from `app.models`) instead of the `init-db` create_all bootstrap used for dev.

---

## Implemented vs. extension points

**Implemented end-to-end:** auth + RBAC + per-document ACL, multi-tenant isolation,
upload (validation, MIME sniffing, dedupe, bulk) → object storage, the 7-stage
resumable pipeline, classification/summary/extraction (deepagents/direct/heuristic),
normalization + typed structured search, chunking + pgvector embeddings, hybrid
(vector+FTS+RRF) permission-aware search, grounded streaming RAG with citations +
refusal + audit, review queue (claim/resolve, side-by-side), dashboard, notifications,
audit logs, admin config, live SSE pipeline status.

**Extension points (clearly stubbed):**
- **OCR** for scanned PDFs/images — parsing captures native text + PDF bounding boxes;
  wire tesseract or Azure Document Intelligence in `services/parsing.py:_parse_image`.
- **In-viewer PDF highlight** — the viewer deep-links to the source page (`#page=N`);
  full pdf.js text-layer highlighting uses the bounding boxes already captured at ingest.
- **Email notifications** — the notification event model is channel-ready; only in-app is wired.
- **GCS backend** — interface present; implement `storage/gcs.py`.
```
